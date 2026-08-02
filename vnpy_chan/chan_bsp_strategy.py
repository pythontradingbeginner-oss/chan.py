# -*- coding: utf-8 -*-
"""ChanBspStrategy: Chan Theory BSP Trend Strategy as vnpy CtaTemplate.

This is a thin adapter that wraps the entire chan.py signal pipeline
inside a standard CtaTemplate so it appears in vnpy's strategy dropdown.

Flow per N-minute bar:
  BarData -> CKLine_Unit -> CChan.trigger_load()
  -> GradedChanStrategy.evaluate_bar() -> SignalDecision -> signal
  -> ExitManager.check() (if position held)
  -> RiskManager.approve()
  -> vnpy buy/sell/short/cover

Place this file in: C:\\Users\\Administrator\\strategies\\
Ensure chan.py project is in PYTHONPATH.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
import pandas as pd

from vnpy_ctastrategy import (
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)
from vnpy.trader.constant import Interval, Offset

# Ensure chan.py project is in sys.path
_CHAN_CANDIDATES = [
    Path("H:/Github/chan.py"),
    Path.home() / "Github" / "chan.py",
    Path(__file__).resolve().parent.parent,
]
for _cp in _CHAN_CANDIDATES:
    if _cp.exists() and str(_cp) not in sys.path:
        sys.path.insert(0, str(_cp))

from chan_futures.trade_intent import DecisionTraceRecord, PendingEntry, TradeIntent
from strategy_policy.position import PositionContext

# Config defaults
DEFAULT_CHAN_PROJECT = "H:/Github/chan.py"
DEFAULT_CONFIG_YAML = "configs/rb_15m_trend_ideal.yaml"


class ChanBspStrategy(CtaTemplate):
    """Chan Theory BSP Trend Strategy (CtaTemplate adapter).

    Triggers on each N-minute bar:
      1. bar -> CKLine_Unit -> CChan.trigger_load()
      2. GradedChanStrategy.evaluate_bar() -> SignalDecision -> signal
      3. If holding a position: ExitManager.check() -> exit?
      4. If no position and signal triggers: buy/short
      5. If signal reverses: sell/cover
    """

    author = "chan.py"

    # --- vnpy parameters (adjustable in GUI) ---
    chan_project_path = DEFAULT_CHAN_PROJECT
    config_yaml = DEFAULT_CONFIG_YAML
    kl_window = 15
    fixed_size = 1
    load_days = 30

    # --- vnpy variables (displayed in GUI) ---
    bi_count = 0
    seg_count = 0
    zs_count = 0
    last_bsp_type = ""
    last_bsp_direction = ""
    last_signal_grade = ""
    bars_processed = 0
    exit_reason = ""
    total_trades = 0
    total_pnl = 0.0
    peak_equity = 0.0

    parameters = [
        "chan_project_path",
        "config_yaml",
        "kl_window",
        "fixed_size",
        "load_days",
    ]
    variables = [
        "bi_count",
        "seg_count",
        "zs_count",
        "last_bsp_type",
        "last_bsp_direction",
        "last_signal_grade",
        "bars_processed",
        "exit_reason",
        "total_trades",
        "total_pnl",
        "peak_equity",
    ]

    # --- Constructor ---

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # Ensure project path is importable
        for p in [self.chan_project_path, DEFAULT_CHAN_PROJECT]:
            if p and Path(p).exists() and str(p) not in sys.path:
                sys.path.insert(0, str(p))

        # BarGenerator for N-minute bar synthesis
        self.bg = BarGenerator(self.on_bar, self.kl_window, self._on_kl_bar)

        # Lazy-initialized chan components
        self._chan = None
        self._wrapper = None
        self._extractor = None
        self._decision_kernel = None
        self._exit_manager = None
        self._risk = None
        self._config = None

        self._position_context: PositionContext | None = None
        self._pending_entry: PendingEntry | None = None
        self._pending_reversal: PendingEntry | None = None
        self._trade_pnl_batch: list[float] = []
        self._previous_close: float | None = None
        self._true_ranges: list[float] = []

    # ============================================================
    # vnpy Lifecycle
    # ============================================================

    def on_init(self):
        """Initialize: load historical bars and warm up CChan."""
        self.write_log(f"ChanBspStrategy init: {self.vt_symbol}")
        self._init_chan_pipeline()

        if self.load_days > 0:
            self.write_log(f"Loading {self.load_days} days of historical bars...")
            self.load_bar(self.load_days, interval=Interval.MINUTE)

        cfg_info = ""
        if self._config:
            cfg_info = (
                f" grade={self._config.grading.min_grade.value}"
                f" exits={[e.type for e in self._config.exits]}"
            )
        self.write_log(f"ChanBspStrategy ready.{cfg_info}")

    def on_start(self):
        """Start trading."""
        self.write_log("ChanBspStrategy started")

    def on_stop(self):
        """Stop trading."""
        self.write_log(
            f"ChanBspStrategy stopped: "
            f"trades={self.total_trades} pnl={self.total_pnl:.0f}pts"
        )

    # ============================================================
    # Bar handling
    # ============================================================

    def on_tick(self, tick: TickData):
        """Tick -> BarGenerator."""
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """1-min bar -> BarGenerator -> N-min bar."""
        self.bg.update_bar(bar)

    def _on_kl_bar(self, bar: BarData):
        """N-minute bar completed. Main strategy logic."""
        self.bars_processed += 1

        if self._chan is None or self._decision_kernel is None:
            return

        # 1. Feed bar to CChan
        klu = self._bar_to_klu(bar)
        self._chan.trigger_load({klu.kl_type: [klu]})

        price = float(bar.close_price)
        atr = self._update_atr(bar)
        timestamp = (
            bar.datetime
            if isinstance(bar.datetime, datetime)
            else pd.Timestamp(bar.datetime)
        )
        self._decision_kernel.observe_structure(
            chan=self._chan,
            timestamp=timestamp,
            lv_idx=0,
        )

        # 2. Update GUI display
        self._update_gui_vars()

        # 3. Check exit rules
        if self.pos != 0 and self._exit_manager is not None:
            exit_sig = self._check_exit(bar, price, timestamp)
            if exit_sig is not None:
                self._pending_reversal = None
                self.exit_reason = exit_sig.reason_code
                self._close_existing(
                    bar, exit_sig.reason_code, price=exit_sig.exit_price
                )
                self.put_event()
                return

        # 4. Evaluate the same TradeIntent whether flat or already positioned.
        intent = self._decision_kernel.evaluate_bar(
            chan=self._chan,
            current_position=int(self.pos),
            price=price,
            timestamp=timestamp,
            active_symbol=bar.symbol,
            lv_idx=0,
            account_equity=self._account_equity(),
            atr=atr,
        )
        if intent is not None:
            self.last_signal_grade = intent.grade
            if not intent.accepted:
                reasons = ",".join(intent.decision.reason_codes)
                self.write_log(f"Decision rejected: {reasons}")
                self.put_event()
                return

            signal = intent.signal
            if self._risk is not None:
                risk_decision = self._risk.approve(signal)
                if not risk_decision.approved:
                    self.write_log(f"Risk rejected: {risk_decision.reason}")
                    self.put_event()
                    return

            if self.pos == 0:
                self._submit_open_intent(intent)
            elif signal.target_position == 0:
                self._pending_reversal = None
                self.exit_reason = "strategy_close"
                self._close_existing(bar, "strategy_close")
            elif (self.pos > 0) != (signal.target_position > 0):
                self._pending_reversal = PendingEntry(intent)
                self.exit_reason = "strategy_reverse"
                self._close_existing(bar, "strategy_reverse")

        self.put_event()

    # ============================================================
    # Order/Trade callbacks
    # ============================================================

    def on_trade(self, trade: TradeData):
        """Track PnL on close."""
        if trade.offset == Offset.OPEN:
            fill_volume = int(trade.volume)
            pending = self._pending_entry
            if pending is None:
                self.write_log("OPEN FILL IGNORED: missing TradeIntent")
                self.put_event()
                return

            opening_position = self._position_context is None
            if opening_position:
                self._position_context = pending.intent.position_from_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None) or datetime.now(),
                    entry_bar=self.bars_processed,
                    active_symbol=getattr(trade, "symbol", None),
                )
            else:
                self._position_context = self._position_context.merge_open_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None),
                )
            self._pending_entry = pending.apply_fill(fill_volume)
            if self._pending_entry.complete:
                self._pending_entry = None
            if self._exit_manager is not None:
                if opening_position:
                    self._exit_manager.on_position_opened(self._position_context)
                else:
                    self._exit_manager.on_position_updated(self._position_context)
            self.write_log(
                f"OPEN FILLED: @ {trade.price:.1f} volume={fill_volume}"
            )
            self.put_event()
            return

        if self._position_context is not None and trade.offset == Offset.CLOSE:
            fill_volume = int(trade.volume)
            fee = self._config.execution.fee_points if self._config else 1.0
            pnl = self._position_context.pnl_points(
                exit_price=float(trade.price),
                fee_points=fee,
                volume=fill_volume,
            )
            self.total_pnl += pnl
            self.total_trades += 1
            self._trade_pnl_batch.append(pnl)
            self._position_context = self._position_context.reduce_volume(fill_volume)

            if self._risk is not None:
                self._risk.on_fill(pnl_points=pnl, fill_time=datetime.now())

            self.write_log(
                f"CLOSE: @ {trade.price:.1f} PnL={pnl:.0f}pts "
                f"cumulative={self.total_pnl:.0f}pts trades={self.total_trades}"
            )
            if self._position_context is None:
                self._pending_entry = None
                self.exit_reason = ""
                if self._exit_manager is not None:
                    self._exit_manager.on_close()
                pending_reversal = self._pending_reversal
                self._pending_reversal = None
                if pending_reversal is not None:
                    self._submit_open_intent(pending_reversal.intent)
            elif self._exit_manager is not None:
                self._exit_manager.on_position_updated(self._position_context)

        self.put_event()

    def on_order(self, order: OrderData):
        pass

    def on_stop_order(self, stop_order: StopOrder):
        pass

    # ============================================================
    # Internal: initialize chan pipeline (lazy)
    # ============================================================

    def _init_chan_pipeline(self):
        """Import and construct all chan.py components."""
        try:
            from Chan import CChan
            from ChanConfig import CChanConfig
            from Common.CEnum import AUTYPE, KL_TYPE

            from chan_futures.config_loader import (
                load_config,
                make_exit_manager,
                make_runtime_decision_kernel,
            )
            from chan_futures.risk import RiskConfig, RiskManager
        except ImportError as e:
            self.write_log(f"Import error: {e}. Check chan_project_path.")
            return

        # Load config
        config_path = Path(self.chan_project_path) / self.config_yaml
        if config_path.exists():
            self._config = load_config(str(config_path))
        else:
            from chan_futures.config import StrategyConfig
            self._config = StrategyConfig()

        cfg = self._config

        # KL_TYPE mapping
        w2k = {
            1: KL_TYPE.K_1M, 5: KL_TYPE.K_5M, 15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M, 60: KL_TYPE.K_60M,
        }
        kl_type = w2k.get(self.kl_window, KL_TYPE.K_15M)

        # CChan
        self._chan = CChan(
            code=self.vt_symbol,
            begin_time=None,
            end_time=None,
            data_src="csv",
            lv_list=[kl_type],
            config=CChanConfig(cfg.chan.to_dict()),
            autype=AUTYPE.NONE,
        )

        # Shared decision kernel
        tf_str = kl_type.name.replace("K_", "").replace("M", "m")
        self._decision_kernel = make_runtime_decision_kernel(
            cfg,
            symbol="RB",
            timeframe=tf_str,
        )
        self._wrapper = self._decision_kernel.strategy
        self._extractor = self._decision_kernel.extractor

        # ExitManager
        self._exit_manager = make_exit_manager(cfg)

        # RiskManager
        self._risk = RiskManager(
            RiskConfig(
                max_abs_position=cfg.risk.max_abs_position,
                max_loss_points=cfg.risk.max_loss_points,
                daily_loss_limit=cfg.risk.daily_loss_limit,
                max_consecutive_losses=cfg.risk.max_consecutive_losses,
            )
        )

    # ============================================================
    # Internal: bar conversion
    # ============================================================

    def _bar_to_klu(self, bar: BarData):
        """Convert vnpy BarData to chan.py CKLine_Unit."""
        from Common.CEnum import DATA_FIELD, KL_TYPE
        from Common.CTime import CTime
        from KLine.KLine_Unit import CKLine_Unit

        ts = pd.Timestamp(bar.datetime)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)

        w2k = {
            1: KL_TYPE.K_1M, 5: KL_TYPE.K_5M, 15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M, 60: KL_TYPE.K_60M,
        }
        kl_type = w2k.get(self.kl_window, KL_TYPE.K_15M)

        item = {
            DATA_FIELD.FIELD_TIME: CTime(
                ts.year, ts.month, ts.day,
                ts.hour, ts.minute, ts.second,
                auto=False,
            ),
            DATA_FIELD.FIELD_OPEN: float(bar.open_price),
            DATA_FIELD.FIELD_HIGH: float(bar.high_price),
            DATA_FIELD.FIELD_LOW: float(bar.low_price),
            DATA_FIELD.FIELD_CLOSE: float(bar.close_price),
            DATA_FIELD.FIELD_VOLUME: float(getattr(bar, "volume", 0) or 0),
        }
        klu = CKLine_Unit(item, autofix=True)
        klu.kl_type = kl_type
        return klu

    # ============================================================
    # Exit checking
    # ============================================================

    def _check_exit(self, bar: BarData, price: float, timestamp):
        """Run ExitManager.check() for current position."""
        if self._exit_manager is None or not self._exit_manager.is_active:
            return None

        return self._exit_manager.check(
            bar_end_time=timestamp,
            open=float(bar.open_price),
            high=float(bar.high_price),
            low=float(bar.low_price),
            close=price,
            opposite_signal_triggered=False,
            chan_snapshot=None,
        )

    def _close_existing(
        self, bar: BarData, reason: str, *, price: float | None = None
    ):
        """Close current position."""
        order_price = float(bar.close_price) if price is None else float(price)
        if self.pos > 0:
            self.sell(order_price, abs(self.pos))
        elif self.pos < 0:
            self.cover(order_price, abs(self.pos))

    def _submit_open_intent(self, intent: TradeIntent) -> None:
        signal = intent.signal
        planned_size = abs(signal.target_position)
        if planned_size <= 0:
            self.write_log("Decision rejected: position_size_zero")
            return

        self._pending_entry = PendingEntry(intent)
        direction_str = self._get_signal_direction(signal)
        if direction_str == "long":
            order_ids = self.buy(signal.price, planned_size)
        else:
            order_ids = self.short(signal.price, planned_size)
        if not order_ids:
            self._pending_entry = None
            return
        self.write_log(
            f"ENTRY: {intent.bsp_type} grade={intent.grade} "
            f"dir={direction_str} @ {signal.price:.1f}"
        )

    def _account_equity(self) -> float | None:
        fallback = self._config.sizing.capital if self._config else None
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is None:
            return fallback
        try:
            oms = main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            balances = [float(account.balance) for account in accounts]
            return sum(balances) if balances else fallback
        except Exception:
            return fallback

    def _update_atr(self, bar: BarData) -> float | None:
        high = float(bar.high_price)
        low = float(bar.low_price)
        close = float(bar.close_price)
        tr = high - low
        if self._previous_close is not None:
            tr = max(tr, abs(high - self._previous_close), abs(low - self._previous_close))
        self._previous_close = close
        self._true_ranges.append(tr)
        period = self._config.sizing.atr_period if self._config else 20
        if len(self._true_ranges) > period:
            self._true_ranges = self._true_ranges[-period:]
        if len(self._true_ranges) < period:
            return None
        return sum(self._true_ranges) / period

    @property
    def position_context(self) -> PositionContext | None:
        return self._position_context

    @property
    def decision_trace(self) -> tuple[DecisionTraceRecord, ...]:
        if self._decision_kernel is None:
            return ()
        return self._decision_kernel.decision_trace

    @property
    def decomposition_state(self):
        if self._decision_kernel is None:
            return None
        return self._decision_kernel.decomposition_state

    @property
    def decomposition_transitions(self):
        if self._decision_kernel is None:
            return ()
        return self._decision_kernel.decomposition_transitions

    # ============================================================
    # GUI variable update
    # ============================================================

    def _update_gui_vars(self):
        """Refresh vnpy GUI monitoring variables."""
        try:
            kl = self._chan[0]
            self.bi_count = len(kl.bi_list) if kl.bi_list else 0
            self.seg_count = len(kl.seg_list.lst) if kl.seg_list else 0
            self.zs_count = len(kl.zs_list.zs_lst) if kl.zs_list else 0

            latest = self._chan.get_latest_bsp(idx=0, number=1)
            if latest and latest[0]:
                self.last_bsp_type = latest[0].type2str()
                self.last_bsp_direction = "buy" if latest[0].is_buy else "sell"
        except Exception:
            pass

    def _get_signal_direction(self, signal) -> str:
        """Infer direction from the signed target position."""
        if signal.target_position > 0:
            return "long"
        if signal.target_position < 0:
            return "short"
        raise ValueError("开仓目标仓位不能为 0")
