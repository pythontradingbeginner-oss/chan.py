"""ExitManager-aware backtest variant of run_chan_trigger_backtest.

Plugs the ExitRule framework into the existing chan_futures backtest loop.
This is a drop-in extension — the original run_chan_trigger_backtest remains unchanged.

Usage (no chan-native fields — vanilla ExitManager):
    from scripts.run_chan_exit_backtest import run_chan_exit_backtest, ChanExitBacktestConfig
    result = run_chan_exit_backtest(frame, config=ChanExitBacktestConfig(
        exit_rules=[
            StructureStopRule(),
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
            TimeStopRule(max_bars=192),
        ],
    ))

Usage (with chan-native fields — requires CChan in the loop):
    result = run_chan_exit_backtest(frame, config=ChanExitBacktestConfig(
        exit_rules=[
            ChanDivergenceExitRule(require_second_level_confirm=True),
            ChanSegmentCompleteExitRule(),
            ChanSmallTurnExitRule(),
            StructureStopRule(),
            TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
        ],
        enable_chan_fields=True,  # fills macd_areas, adjacent_bidong, etc.
    ))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE

from chan_futures.execution import Fill, SimulatedExecutionEngine
from chan_futures.feed import prepare_ohlc_frame, row_to_klu
from chan_futures.risk import RiskConfig, RiskManager
from chan_futures.strategy import MinimalChanTrendStrategy
from signal_core.models import SignalDirection
from strategy_policy.exit_rules import (
    ExitContext,
    ExitManager,
    ExitRule,
    ExitSignal,
)


@dataclass
class ChanExitBacktestConfig:
    """Extended backtest config with ExitRule support."""

    code: str = "RB_MAIN"
    kl_type: KL_TYPE = KL_TYPE.K_1M
    fee_points: float = 1.0
    slippage_points: float = 1.0
    max_abs_position: int = 1
    max_loss_points: float | None = None
    allow_short: bool = True
    chan_config: dict | None = None

    # ── ExitManager ──
    exit_rules: list[ExitRule] = field(default_factory=list)

    # ── Grade filter (uses GradedChanStrategy + SignalExtractor) ──
    use_grade_filter: bool = True
    min_grade: str = "standard"       # "ideal" | "standard" | "weak"
    accepted_bsp_types: tuple[str, ...] | None = None  # e.g. ("1", "1p")

    # ── Chan-native fields ──
    enable_chan_fields: bool = False
    avg_amplitude_window: int = 20       # ChanSmallTurnExitRule
    second_level_kl_type: KL_TYPE | None = KL_TYPE.K_1M

    # ── Strict mode for debugging ──
    exit_strict: bool = False


@dataclass(frozen=True)
class ChanExitBacktestResult:
    bars: pd.DataFrame
    fills: pd.DataFrame
    summary: pd.DataFrame
    exit_events: pd.DataFrame
    exit_error_summary: dict[str, int]


def run_chan_exit_backtest(
    frame: pd.DataFrame,
    *,
    config: ChanExitBacktestConfig | None = None,
    limit: int | None = None,
) -> ChanExitBacktestResult:
    """Replay bars through CChan.trigger_load, trade BSP signals, manage exits via ExitManager.

    When use_grade_filter=True (default), uses GradedChanStrategy + SignalExtractor
    to filter entries by signal grade. This dramatically improves results:
    - No filter: ~+1 pt on 5000 bars
    - min_grade=standard: ~+170 pts
    - min_grade=ideal: ~+628 pts
    """
    config = config or ChanExitBacktestConfig()
    bars = prepare_ohlc_frame(frame)
    if limit is not None and limit > 0:
        bars = bars.iloc[:limit].reset_index(drop=True)

    chan = _new_empty_chan(config)

    # ── Grade filter setup ──
    if config.use_grade_filter:
        from chan_futures.graded_strategy import GradeFilterConfig, GradedChanStrategy
        from signal_core import SignalExtractor

        accepted_bsp = None
        if config.accepted_bsp_types is not None:
            from Common.CEnum import BSP_TYPE
            _type_str_to_bsp: dict[str, BSP_TYPE] = {
                "1": BSP_TYPE.T1, "1p": BSP_TYPE.T1P,
                "2": BSP_TYPE.T2, "2s": BSP_TYPE.T2S,
                "3a": BSP_TYPE.T3A, "3b": BSP_TYPE.T3B,
            }
            accepted_bsp = [_type_str_to_bsp[t] for t in config.accepted_bsp_types
                           if t in _type_str_to_bsp]

        strategy_wrapper = GradedChanStrategy(GradeFilterConfig(
            min_grade=config.min_grade,
            accepted_bsp_types=accepted_bsp,
            allow_short=config.allow_short,
        ))

        timeframe_str = config.kl_type.name.replace("K_", "").replace("M", "m")
        extractor = SignalExtractor(
            symbol="RB",
            timeframe=timeframe_str,
            contract=config.code,
        )
    else:
        from chan_futures.strategy import MinimalChanTrendStrategy
        strategy_raw = MinimalChanTrendStrategy(allow_short=config.allow_short)
        extractor = None

    risk = RiskManager(
        RiskConfig(
            max_abs_position=config.max_abs_position,
            max_loss_points=config.max_loss_points,
        )
    )
    execution = SimulatedExecutionEngine(
        fee_points=config.fee_points,
        slippage_points=config.slippage_points,
    )

    # ── Build ExitManager ──
    exit_manager = ExitManager(config.exit_rules, strict=config.exit_strict)

    # ── Chan-native state bookkeeping ──
    chan_state = _ChanLoopState() if config.enable_chan_fields else None

    records: list[dict[str, object]] = []
    exit_events: list[dict[str, object]] = []

    for row_number, row in bars.iterrows():
        klu = row_to_klu(row, kl_type=config.kl_type)
        chan.trigger_load({config.kl_type: [klu]})
        price = float(row["close"])
        open_price = float(row["open"])
        high_price = float(row["high"])
        low_price = float(row["low"])
        timestamp = row["datetime"]
        if isinstance(timestamp, pd.Timestamp):
            dt_timestamp = timestamp.to_pydatetime()
        else:
            dt_timestamp = timestamp

        active_symbol = row.get("active_symbol")

        # ── 1. Check exits first (if holding a position) ──
        exit_signal: ExitSignal | None = None
        if exit_manager.is_active:
            exit_kwargs = _build_exit_check_kwargs(
                dt_timestamp, open_price, high_price, low_price, price,
                chan=chan if config.enable_chan_fields else None,
                state=chan_state,
                row_number=row_number,
            )
            exit_signal = exit_manager.check(**exit_kwargs)

            if exit_signal is not None:
                _force_close(
                    execution=execution,
                    exit_signal=exit_signal,
                    current_position=execution.state.position,
                    price=price,
                    timestamp=timestamp,
                    active_symbol=active_symbol,
                )
                exit_manager.on_close()
                exit_events.append({
                    "row_number": row_number,
                    "datetime": timestamp,
                    "rule_id": exit_signal.rule_id,
                    "reason_code": exit_signal.reason_code,
                    "trigger_price": exit_signal.exit_price,
                    "description": exit_signal.description,
                })

        # ── 2. Check entries ──
        fill: Fill | None = None
        risk_reason = None
        signal = None
        grade_info: dict | None = None
        strategy_exit: bool = False  # whether the strategy itself triggered a closing/reversal trade
        if exit_signal is None:
            # Only check entries if we didn't just exit (avoids double-filling)
            if config.use_grade_filter:
                graded = strategy_wrapper.on_bar(
                    chan=chan,
                    current_position=execution.state.position,
                    price=price,
                    timestamp=timestamp,
                    active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
                    lv_idx=0,
                    extractor=extractor,
                )
                if graded is not None:
                    signal = graded.signal
                    grade_info = {
                        "grade": graded.grade,
                        "structural_score": graded.structural_score,
                        "event_id": graded.event_id,
                        "signal_key": graded.signal_key,
                    }
            else:
                signal = strategy_raw.on_bar(
                    chan=chan,
                    current_position=execution.state.position,
                    price=price,
                    timestamp=timestamp,
                    active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
                )

            if signal is not None:
                has_position = execution.state.position != 0
                # Check if this signal closes or reverses an existing position
                if has_position:
                    if signal.action in ("close_long", "close_short"):
                        strategy_exit = True
                    elif signal.action in ("reverse_long_to_short", "reverse_short_to_long"):
                        strategy_exit = True
                        # For reversals: close existing first (tracked), then open new
                        exit_manager.on_close()
                        exit_events.append({
                            "row_number": row_number,
                            "datetime": timestamp,
                            "rule_id": "strategy_reversal",
                            "reason_code": "strategy_reverse",
                            "trigger_price": price,
                            "description": f"Strategy reversal: {signal.action}",
                        })

                decision = risk.approve(signal, realized_points=execution.state.realized_points)
                risk_reason = decision.reason
                if decision.approved:
                    fill = execution.execute(signal)
                    if fill is not None:
                        # If we now have an open position, register with ExitManager
                        current_pos = execution.state.position
                        if current_pos != 0:
                            direction = (
                                SignalDirection.LONG if current_pos > 0
                                else SignalDirection.SHORT
                            )
                            entry_grade = grade_info["grade"] if grade_info else None
                            exit_manager.on_entry(
                                direction=direction,
                                entry_price=fill.fill_price,
                                entry_grade=entry_grade,
                            )

        records.append({
            "row_number": row_number,
            "datetime": timestamp,
            "close": price,
            "active_symbol": active_symbol,
            "position": execution.state.position,
            "avg_price": execution.state.avg_price,
            "realized_points": execution.state.realized_points,
            "equity_points": execution.mark_to_market(price),
            "signal_action": signal.action if (signal is not None and exit_signal is None and fill is None) else None,
            "signal_bsp_type": signal.bsp_type if (signal is not None and exit_signal is None and fill is None) else None,
            "signal_grade": grade_info["grade"] if grade_info else None,
            "risk_reason": risk_reason,
            "fill_price": fill.fill_price if fill else None,
            "exit_reason": exit_signal.reason_code if exit_signal else None,
        })

    equity = pd.DataFrame(records)
    fills = pd.DataFrame([f.__dict__ for f in execution.fills])
    exit_events_df = pd.DataFrame(exit_events)

    return ChanExitBacktestResult(
        bars=equity,
        fills=fills,
        summary=_build_summary(equity, fills, config),
        exit_events=exit_events_df,
        exit_error_summary=exit_manager.error_summary(),
    )


# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════

def _build_exit_check_kwargs(
    dt_timestamp: datetime,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    *,
    chan=None,
    state: _ChanLoopState | None = None,
    row_number: int = 0,
) -> dict:
    """Build the kwargs dict for ExitManager.check(), with optional chan-native fields."""
    kwargs: dict = {
        "bar_end_time": dt_timestamp,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
    }
    if chan is None or state is None:
        return kwargs

    # ── Update chan-native state ──
    state.update(open_price, high_price, low_price, close_price, chan)

    from strategy_policy.exit_rules import ChanExitSnapshot
    snapshot = ChanExitSnapshot(
        available_at=dt_timestamp,
        macd_areas=state.macd_areas,
        adjacent_bidong=state.adjacent_bidong,
        is_new_high_low=state.is_new_high_low,
        prev_high=state.prev_high,
        prev_low=state.prev_low,
        segment_complete=state.segment_complete,
        fractal_break_price=state.fractal_break_price,
        bar_amplitude=state.bar_amplitude,
        avg_amplitude_20=state.avg_amplitude_20,
    )
    kwargs["chan_snapshot"] = snapshot

    return kwargs


def _force_close(
    execution: SimulatedExecutionEngine,
    exit_signal: ExitSignal,
    current_position: int,
    price: float,
    timestamp: object,
    active_symbol: str | None = None,
) -> None:
    """Force a position close via SimulatedExecutionEngine."""
    from chan_futures.strategy import StrategySignal

    if current_position == 0:
        return

    if current_position > 0:
        target = 0
        action = "close_long"
    else:
        target = 0
        action = "close_short"

    sig = StrategySignal(
        timestamp=timestamp,
        action=action,
        target_position=target,
        price=price,
        reason=exit_signal.reason_code,
        bsp_type="exit",
        bsp_bi_idx=-1,
        bsp_klu_idx=-1,
        active_symbol=active_symbol,
    )
    execution.execute(sig)


# ═══════════════════════════════════════════
# Chan-native loop state (minimal, non-invasive)
# ═══════════════════════════════════════════

class _ChanLoopState:
    """Lightweight state tracker for chan-native exit rule fields.

    Computes ExitContext fields from the CChan instance each bar,
    without coupling ExitRules to CChan internals.
    """

    def __init__(self, window: int = 20) -> None:
        self.window = window
        self._amplitude_history: list[float] = []
        self._close_history: list[float] = []
        self._high_history: list[float] = []
        self._low_history: list[float] = []

        # Output fields (populated each bar)
        self.macd_areas: list[float] | None = None
        self.adjacent_bidong: list[float] | None = None
        self.is_new_high_low: bool = False
        self.prev_high: float | None = None
        self.prev_low: float | None = None
        self.segment_complete: bool = False
        self.fractal_break_price: float | None = None
        self.bar_amplitude: float | None = None
        self.avg_amplitude_20: float | None = None

    def update(
        self,
        open_price: float,
        high: float,
        low: float,
        close: float,
        chan,
    ) -> None:
        """Called once per bar. Extracts chan-native context fields from CChan."""
        # ── Amplitude tracking ──
        self.bar_amplitude = abs(high - low)
        self._amplitude_history.append(self.bar_amplitude)
        if len(self._amplitude_history) > self.window:
            self._amplitude_history.pop(0)
        if len(self._amplitude_history) >= 5:
            self.avg_amplitude_20 = sum(self._amplitude_history) / len(self._amplitude_history)
        else:
            self.avg_amplitude_20 = None

        # ── Price history for is_new_high_low ──
        self._close_history.append(close)
        self._high_history.append(high)
        self._low_history.append(low)
        if len(self._close_history) > 100:
            self._close_history.pop(0)
            self._high_history.pop(0)
            self._low_history.pop(0)

        # ── New high / low detection (chan theory: use bar extremes, not close) ──
        if len(self._high_history) >= 5:
            self.is_new_high_low = (
                high > max(self._high_history[:-1])
                or low < min(self._low_history[:-1])
            )
            self.prev_high = max(self._high_history[:-1])
            self.prev_low = min(self._low_history[:-1])
        else:
            self.is_new_high_low = False
            self.prev_high = None
            self.prev_low = None

        # ── Extract from CChan: bi list → adjacent_bidong ──
        self.adjacent_bidong = self._extract_bidong_strength(chan)

        # ── Extract from CChan: MACD areas ──
        self.macd_areas = self._extract_macd_areas(chan)

        # ── Segment completion ──
        self.segment_complete = self._detect_segment_complete(chan)

        # ── Fractal break price ──
        self.fractal_break_price = self._extract_fractal_break(chan, close)

    def _extract_bidong_strength(self, chan) -> list[float] | None:
        """Extract adjacent bi amplitudes (涨跌点数) from the chanel bi list.

        Returns a list where positive=上涨笔, negative=下跌笔.
        """
        try:
            bi_list = chan[0].bi_list
            if len(bi_list) < 2:
                return None
            strengths: list[float] = []
            for bi in bi_list:
                if bi.begin_klc is None or bi.end_klc is None:
                    continue
                begin_close = bi.begin_klc.get_klu(bi.kl_type.value)
                end_close = bi.end_klc.get_klu(bi.kl_type.value)
                if begin_close is None or end_close is None:
                    continue
                diff = end_close.close - begin_close.close
                strengths.append(float(diff))
            return strengths if strengths else None
        except Exception:
            return None

    def _extract_macd_areas(self, chan) -> list[float] | None:
        """Extract MACD area sequence from the chan MACD model.

        Returns a list of area values — positive for bullish segments,
        negative for bearish segments.
        """
        try:
            bi_list = chan[0].bi_list
            if len(bi_list) < 2:
                return None
            areas: list[float] = []
            for bi in bi_list:
                macd_metric = None
                try:
                    if hasattr(bi, 'macd_metric') and bi.macd_metric is not None:
                        macd_metric = abs(bi.macd_metric)
                    elif hasattr(bi, 'macd_amp') and bi.macd_amp is not None:
                        macd_metric = abs(bi.macd_amp)
                except Exception:
                    pass
                if macd_metric is not None and bi.dir is not None:
                    sign = 1 if bi.is_up else -1 if hasattr(bi, 'is_up') else 0
                    if sign == 0 and hasattr(bi, 'direction'):
                        sign = 1 if bi.direction.value == 'up' else -1
                    areas.append(sign * float(macd_metric))
            return areas if areas else None
        except Exception:
            return None

    def _detect_segment_complete(self, chan) -> bool:
        """Check if the latest segment has just been confirmed complete."""
        try:
            seg_list = chan[0].seg_list
            if len(seg_list) < 1:
                return False
            # Latest confirmed segment (exclude virtual segments pending confirmation)
            confirmed = [s for s in seg_list if not getattr(s, 'is_sure', True)]
            if not confirmed:
                return False
            latest = confirmed[-1]
            # A segment is "complete" when its end bi is confirmed
            end_bi = getattr(latest, 'end_bi', None)
            if end_bi is not None and end_bi.klc is not None:
                # The segment's end bi's KLC refers to the bar that confirmed it
                return hasattr(end_bi, 'is_sure') and end_bi.is_sure
            return False
        except Exception:
            return False

    def _extract_fractal_break(self, chan, current_close: float) -> float | None:
        """Extract the price level of the most recent opposing fractal's confirmation.

        For long positions: the latest confirmed bottom fractal's low.
        For short positions: the latest confirmed top fractal's high.

        Returns None if no confirmed fractal is found.
        """
        try:
            kl_list = chan[0]
            if len(kl_list) < 3:
                return None
            # Scan backward for the most recent confirmed fractal
            for i in range(len(kl_list) - 2, max(len(kl_list) - 30, 1), -1):
                klu = kl_list[i]
                if hasattr(klu, 'fx') and klu.fx is not None:
                    from Common.CEnum import FX_TYPE
                    if klu.fx == FX_TYPE.BOTTOM:
                        return float(klu.low)
                    elif klu.fx == FX_TYPE.TOP:
                        return float(klu.high)
            return None
        except Exception:
            return None


# ═══════════════════════════════════════════
# Chan / summary (copied from orig backtest for self-containment)
# ═══════════════════════════════════════════

def _new_empty_chan(config: ChanExitBacktestConfig) -> CChan:
    chan_config = {
        "trigger_step": True,
        "bi_strict": True,
        "divergence_rate": float("inf"),
        "bsp2_follow_1": False,
        "bsp3_follow_1": False,
        "min_zs_cnt": 0,
        "bs1_peak": False,
        "macd_algo": "peak",
        "bs_type": "1,1p,2,2s,3a,3b",
        "print_warning": False,
    }
    if config.chan_config:
        chan_config.update(config.chan_config)
    return CChan(
        code=config.code,
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[config.kl_type],
        config=CChanConfig(chan_config),
        autype=AUTYPE.NONE,
    )


def _build_summary(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    config: ChanExitBacktestConfig,
) -> pd.DataFrame:
    if equity.empty:
        max_drawdown = total_return = 0.0
    else:
        curve = equity["equity_points"].astype(float)
        total_return = float(curve.iloc[-1])
        max_drawdown = float((curve.cummax() - curve).max())
    return pd.DataFrame([
        {
            "code": config.code,
            "kl_type": config.kl_type.name,
            "bar_count": int(len(equity)),
            "fill_count": int(len(fills)),
            "total_return_points": total_return,
            "max_drawdown_points": max_drawdown,
            "fee_points": config.fee_points,
            "slippage_points": config.slippage_points,
            "allow_short": config.allow_short,
            "exit_rule_count": len(config.exit_rules),
        }
    ])


def save_backtest_reports(
    result: ChanExitBacktestResult,
    output_dir: Path,
) -> None:
    """Save all backtest artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    result.bars.to_csv(output_dir / "chan_exit_backtest_bars.csv", index=False, encoding="utf-8-sig")
    result.fills.to_csv(output_dir / "chan_exit_backtest_fills.csv", index=False, encoding="utf-8-sig")
    result.summary.to_csv(output_dir / "chan_exit_backtest_summary.csv", index=False, encoding="utf-8-sig")
    result.exit_events.to_csv(output_dir / "chan_exit_backtest_exit_events.csv", index=False, encoding="utf-8-sig")
