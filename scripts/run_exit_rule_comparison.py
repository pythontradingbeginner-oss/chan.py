"""Phase A: Exit Rule Comparison — Grade-filtered strategy baseline vs +exit rules.

Tests whether adding exit rules (trailing stop, time stop, structure stop, etc.)
improves upon the grade-filtered strategy's reversal-only exits.

Produces: reports/exit_rule_comparison/
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings("ignore")

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, BSP_TYPE, DATA_SRC, KL_TYPE
from chan_futures.execution import Fill, SimulatedExecutionEngine
from chan_futures.feed import prepare_ohlc_frame, row_to_klu
from chan_futures.graded_strategy import GradedChanStrategy, GradeFilterConfig
from chan_futures.risk import RiskConfig, RiskManager
from signal_core import SignalExtractor
from signal_core.models import SignalDirection
from strategy_policy.exit_rules import (
    ExitManager,
    ExitRule,
    ExitSignal,
    StructureStopRule,
    FixedStopRule,
    TrailingStopRule,
    TimeStopRule,
    OppositeSignalRule,
)


# ════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════

@dataclass
class CombinedBacktestConfig:
    code: str = "RB_MAIN"
    kl_type: KL_TYPE = KL_TYPE.K_15M
    fee_points: float = 1.0
    slippage_points: float = 1.0
    max_abs_position: int = 1
    allow_short: bool = True

    # Grade filter
    use_grade_filter: bool = True
    min_grade: str = "ideal"
    accepted_bsp_types: tuple[str, ...] | None = None

    # Exit rules (empty list = strategy reversal only)
    exit_rules: list[ExitRule] = field(default_factory=list)
    exit_strict: bool = False


@dataclass
class CombinedBacktestResult:
    config_label: str
    bars: pd.DataFrame
    fills: pd.DataFrame
    exit_events: pd.DataFrame
    exit_error_summary: dict[str, int]
    trades: list[dict[str, Any]]
    summary: dict[str, Any]


# ════════════════════════════════════════════════════════════════
# Core engine
# ════════════════════════════════════════════════════════════════

_TYPE_STR_TO_BSP = {
    "1": BSP_TYPE.T1, "1p": BSP_TYPE.T1P,
    "2": BSP_TYPE.T2, "2s": BSP_TYPE.T2S,
    "3a": BSP_TYPE.T3A, "3b": BSP_TYPE.T3B,
}


def _new_chan(config: CombinedBacktestConfig) -> CChan:
    chan_cfg = {
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
    return CChan(
        code=config.code,
        begin_time=None, end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[config.kl_type],
        config=CChanConfig(chan_cfg),
        autype=AUTYPE.NONE,
    )


def _setup_strategy(config: CombinedBacktestConfig):
    """Return (strategy_wrapper, extractor, strategy_raw)."""
    if config.use_grade_filter:
        accepted_bsp = None
        if config.accepted_bsp_types is not None:
            accepted_bsp = [_TYPE_STR_TO_BSP[t] for t in config.accepted_bsp_types
                           if t in _TYPE_STR_TO_BSP]
        wrapper = GradedChanStrategy(GradeFilterConfig(
            min_grade=config.min_grade,
            accepted_bsp_types=accepted_bsp,
            allow_short=config.allow_short,
        ))
        timeframe_str = config.kl_type.name.replace("K_", "").replace("M", "m")
        extractor = SignalExtractor(symbol="RB", timeframe=timeframe_str, contract=config.code)
        return wrapper, extractor, None
    else:
        from chan_futures.strategy import MinimalChanTrendStrategy
        raw = MinimalChanTrendStrategy(allow_short=config.allow_short)
        return None, None, raw


def run_combined_backtest(
    frame: pd.DataFrame,
    *,
    config: CombinedBacktestConfig | None = None,
    config_label: str = "",
    limit: int | None = None,
) -> CombinedBacktestResult:
    """Run a single backtest pass with grade filter + exit rules."""
    config = config or CombinedBacktestConfig()
    bars_df = prepare_ohlc_frame(frame)
    if limit is not None and limit > 0:
        bars_df = bars_df.iloc[:limit].reset_index(drop=True)

    chan = _new_chan(config)
    wrapper, extractor, strategy_raw = _setup_strategy(config)

    risk = RiskManager(RiskConfig(
        max_abs_position=config.max_abs_position,
        max_loss_points=None,
    ))
    execution = SimulatedExecutionEngine(
        fee_points=config.fee_points,
        slippage_points=config.slippage_points,
    )

    # Build ExitManager (if rules provided)
    exit_manager = ExitManager(
        config.exit_rules if config.exit_rules else [],
        strict=config.exit_strict,
    )
    use_exit_rules = len(config.exit_rules) > 0

    # ── State tracking for trade reconstruction ──
    records: list[dict] = []
    exit_events: list[dict] = []
    trades: list[dict] = []
    current_trade: dict | None = None

    def _close_trade(exit_bar: int, exit_price: float, exit_time, reason: str,
                     rule_id: str = "strategy_reverse"):
        nonlocal current_trade
        if current_trade is None:
            return
        pnl = exit_price - current_trade["entry_price"]
        if current_trade["direction"] == "short":
            pnl = -pnl
        pnl -= config.fee_points * 2  # entry + exit fees
        current_trade.update({
            "exit_bar": exit_bar,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "hold_bars": exit_bar - current_trade["entry_bar"],
            "pnl_points": round(pnl, 1),
            "exit_reason": reason,
            "exit_rule": rule_id,
        })
        trades.append(current_trade)
        current_trade = None

    for row_number, row in bars_df.iterrows():
        klu = row_to_klu(row, kl_type=config.kl_type)
        chan.trigger_load({config.kl_type: [klu]})
        price = float(row["close"])
        high_price = float(row["high"])
        low_price = float(row["low"])
        timestamp = row["datetime"]
        if isinstance(timestamp, pd.Timestamp):
            dt_timestamp = timestamp.to_pydatetime()
        else:
            dt_timestamp = timestamp

        active_symbol = row.get("active_symbol")

        # ── 1. Check exit rules (if active) ──
        exit_signal: ExitSignal | None = None
        if use_exit_rules and exit_manager.is_active:
            exit_signal = exit_manager.check(
                bar_end_time=dt_timestamp,
                open=float(row["open"]),
                high=high_price,
                low=low_price,
                close=price,
            )

            if exit_signal is not None:
                if execution.state.position != 0:
                    _close_trade(
                        exit_bar=row_number, exit_price=exit_signal.exit_price,
                        exit_time=timestamp, reason=exit_signal.reason_code,
                        rule_id=exit_signal.rule_id,
                    )
                # Force close via execution engine
                from chan_futures.strategy import StrategySignal
                target = 0
                if execution.state.position > 0:
                    action = "close_long"
                elif execution.state.position < 0:
                    action = "close_short"
                else:
                    action = "hold"
                if execution.state.position != 0:
                    sig = StrategySignal(
                        timestamp=timestamp, action=action, target_position=0,
                        price=price, reason=exit_signal.reason_code,
                        bsp_type="exit", bsp_bi_idx=-1, bsp_klu_idx=-1,
                        active_symbol=active_symbol,
                    )
                    execution.execute(sig)
                    exit_manager.on_close()

                exit_events.append({
                    "row_number": row_number,
                    "datetime": timestamp,
                    "rule_id": exit_signal.rule_id,
                    "reason_code": exit_signal.reason_code,
                    "trigger_price": exit_signal.exit_price,
                    "description": exit_signal.description,
                })

        # ── 2. Check strategy entries ──
        if exit_signal is None:
            # Strategy generates signals
            if wrapper is not None:
                graded = wrapper.on_bar(
                    chan=chan, current_position=execution.state.position,
                    price=price, timestamp=timestamp,
                    active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
                    lv_idx=0, extractor=extractor,
                )
                signal = graded.signal if graded is not None else None
                grade_info = {"grade": graded.grade} if graded else None
            else:
                signal = strategy_raw.on_bar(
                    chan=chan, current_position=execution.state.position,
                    price=price, timestamp=timestamp,
                    active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
                )
                grade_info = None

            if signal is not None:
                # Track strategy reversal as a close event
                has_pos = execution.state.position != 0
                if has_pos and signal.action in ("close_long", "close_short",
                    "reverse_long_to_short", "reverse_short_to_long"):
                    _close_trade(
                        exit_bar=row_number, exit_price=price,
                        exit_time=timestamp, reason="strategy_reverse",
                        rule_id="strategy_reversal",
                    )
                    exit_manager.on_close()

                decision = risk.approve(signal, realized_points=execution.state.realized_points)
                if decision.approved:
                    fill = execution.execute(signal)
                    if fill is not None:
                        # Track new entry
                        if fill.target_position != 0:
                            direction = "long" if fill.target_position > 0 else "short"
                            # Deduct entry fee
                            current_trade = {
                                "entry_bar": row_number,
                                "entry_price": fill.fill_price - config.fee_points,
                                "entry_time": timestamp,
                                "direction": direction,
                                "grade": grade_info["grade"] if grade_info else "unknown",
                                "active_symbol": str(active_symbol) if pd.notna(active_symbol) else "",
                            }
                            # Register with ExitManager
                            dir_enum = SignalDirection.LONG if direction == "long" else SignalDirection.SHORT
                            eg = grade_info["grade"] if grade_info else None
                            exit_manager.on_entry(
                                direction=dir_enum,
                                entry_price=fill.fill_price,
                                entry_grade=eg,
                            )

        records.append({
            "row_number": row_number,
            "datetime": timestamp,
            "close": price,
            "position": execution.state.position,
            "avg_price": execution.state.avg_price,
            "realized_points": execution.state.realized_points,
            "equity_points": execution.mark_to_market(price),
            "exit_reason": exit_signal.reason_code if exit_signal else None,
        })

    # Close any open trade at the end
    if current_trade is not None and execution.state.position != 0:
        last_bar = bars_df.iloc[-1]
        _close_trade(
            exit_bar=len(bars_df) - 1,
            exit_price=float(last_bar["close"]),
            exit_time=last_bar["datetime"],
            reason="end_of_data",
            rule_id="end_of_data",
        )

    # ── Compute summary metrics ──
    equity = pd.DataFrame(records)
    fills = pd.DataFrame([f.__dict__ for f in execution.fills])
    exit_evts = pd.DataFrame(exit_events)
    summary = _compute_summary(equity, fills, trades, config_label)

    return CombinedBacktestResult(
        config_label=config_label,
        bars=equity,
        fills=fills,
        exit_events=exit_evts,
        exit_error_summary=exit_manager.error_summary(),
        trades=trades,
        summary=summary,
    )


def _compute_summary(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    trades: list[dict],
    label: str,
) -> dict[str, Any]:
    if equity.empty:
        return {"label": label, "bar_count": 0, "fill_count": 0,
                "trade_count": 0, "total_return": 0.0, "max_drawdown": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0,
                "avg_hold_bars": 0.0, "sharpe": 0.0}

    curve = equity["equity_points"].astype(float)
    total_return = float(curve.iloc[-1])
    max_dd = float((curve.cummax() - curve).max())

    if not trades:
        return {"label": label, "bar_count": len(equity), "fill_count": len(fills),
                "trade_count": 0, "total_return": round(total_return, 1),
                "max_drawdown": round(max_dd, 1),
                "win_rate": 0.0, "profit_factor": 0.0, "avg_hold_bars": 0.0, "sharpe": 0.0}

    win_trades = [t for t in trades if t["pnl_points"] > 0]
    loss_trades = [t for t in trades if t["pnl_points"] <= 0]
    win_rate = len(win_trades) / len(trades)
    total_profit = sum(t["pnl_points"] for t in win_trades)
    total_loss = abs(sum(t["pnl_points"] for t in loss_trades))
    profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
    avg_hold = sum(t["hold_bars"] for t in trades) / len(trades)

    # Sharpe (annualized approximation)
    if len(trades) >= 2:
        returns = [t["pnl_points"] for t in trades]
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = var_r ** 0.5
        sharpe = (mean_r / std_r) * (len(trades) ** 0.5) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # Count exit reasons
    exit_reason_counts: dict[str, int] = {}
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

    return {
        "label": label,
        "bar_count": len(equity),
        "fill_count": len(fills),
        "trade_count": len(trades),
        "total_return": round(total_return, 1),
        "max_drawdown": round(max_dd, 1),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2),
        "avg_hold_bars": round(avg_hold, 1),
        "sharpe": round(sharpe, 3),
        "exit_reasons": exit_reason_counts,
    }


# ════════════════════════════════════════════════════════════════
# Main — comparison grid
# ════════════════════════════════════════════════════════════════

def main() -> None:
    print("=" * 70)
    print("Phase A: Exit Rule Comparison (Grade-Filtered Baseline)")
    print("=" * 70)

    output_dir = Path("reports/exit_rule_comparison")
    output_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.read_parquet("data/processed/RB_15m_continuous_raw.parquet")
    t_min = frame["datetime"].min()
    t_max = frame["datetime"].max()
    print(f"Data: {len(frame)} bars, {t_min} → {t_max}")

    limit_bars = 5000

    # ── Define experiments ──
    experiments: list[tuple[str, CombinedBacktestConfig]] = []

    # Baseline: strategy reversal only (no exit rules)
    for grade, label_grade in [("ideal", "Ideal"), ("standard", "Std")]:
        experiments.append((
            f"baseline_{grade}",
            CombinedBacktestConfig(
                kl_type=KL_TYPE.K_15M,
                fee_points=1.0, slippage_points=1.0,
                use_grade_filter=True,
                min_grade=grade,
                exit_rules=[],
            ),
        ))

    # Strategy + exit rules: various combinations
    exit_combos = {
        "fix180_time192": [
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TimeStopRule(max_bars=192),
        ],
        "fix180_time192_trail500_50": [
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TimeStopRule(max_bars=192),
            TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
        ],
        "fix180_time192_trail300_40": [
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TimeStopRule(max_bars=192),
            TrailingStopRule(trigger_points=300.0, giveback_ratio=0.4),
        ],
        "struct_fix180_time192_trail500_50": [
            StructureStopRule(grade_tighten=True),
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TimeStopRule(max_bars=192),
            TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
        ],
        "struct_fix120_trail400_40_time192": [
            StructureStopRule(grade_tighten=True),
            FixedStopRule(stop_points=120.0, grade_adjust=True),
            TrailingStopRule(trigger_points=400.0, giveback_ratio=0.4),
            TimeStopRule(max_bars=192),
        ],
        "struct_fix250_trail600_40_time288": [
            StructureStopRule(grade_tighten=True),
            FixedStopRule(stop_points=250.0, grade_adjust=True),
            TrailingStopRule(trigger_points=600.0, giveback_ratio=0.4),
            TimeStopRule(max_bars=288),
        ],
    }

    # Test each exit combo at ideal grade, then best at standard too
    for exit_name, rules in exit_combos.items():
        experiments.append((
            f"ideal_{exit_name}",
            CombinedBacktestConfig(
                kl_type=KL_TYPE.K_15M,
                fee_points=1.0, slippage_points=1.0,
                use_grade_filter=True, min_grade="ideal",
                exit_rules=rules,
            ),
        ))

    # ── Run all experiments ──
    all_summaries: list[dict] = []
    for label, cfg in experiments:
        t0 = time.time()
        rules_str = " + ".join(r.rule_id for r in cfg.exit_rules) if cfg.exit_rules else "(strategy reversal only)"
        print(f"\n{'─' * 60}")
        print(f"[{label}]")
        print(f"  Grade: {cfg.min_grade}, Exit rules: {rules_str}")

        result = run_combined_backtest(
            frame, config=cfg, config_label=label, limit=limit_bars,
        )
        elapsed = time.time() - t0

        s = result.summary
        all_summaries.append(s)
        print(f"  {elapsed:.1f}s | Trades={s['trade_count']} Return={s['total_return']}pts "
              f"DD={s['max_drawdown']}pts WR={s['win_rate']:.1%} PF={s['profit_factor']} "
              f"AvgHold={s['avg_hold_bars']:.0f}b")

        # Save full results
        variant_dir = output_dir / label
        variant_dir.mkdir(parents=True, exist_ok=True)
        result.bars.to_csv(variant_dir / "bars.csv", index=False, encoding="utf-8-sig")
        result.fills.to_csv(variant_dir / "fills.csv", index=False, encoding="utf-8-sig")
        result.exit_events.to_csv(variant_dir / "exit_events.csv", index=False, encoding="utf-8-sig")
        if result.trades:
            pd.DataFrame(result.trades).to_csv(variant_dir / "trades.csv", index=False, encoding="utf-8-sig")
        with open(variant_dir / "summary.json", "w") as f:
            json.dump(s, f, indent=2, default=str, ensure_ascii=False)
        with open(variant_dir / "config.json", "w") as f:
            json.dump({
                "label": label,
                "min_grade": cfg.min_grade,
                "use_grade_filter": cfg.use_grade_filter,
                "exit_rules": [r.rule_id for r in cfg.exit_rules],
                "fee_points": cfg.fee_points,
                "slippage_points": cfg.slippage_points,
                "limit_bars": limit_bars,
            }, f, indent=2)

    # ── Print comparison table ──
    print(f"\n{'=' * 85}")
    print(f"{'Exit Rule Comparison — Grade-Filtered Strategy':^85}")
    print(f"{'=' * 85}")
    hdr = f"{'Experiment':<48} {'Trades':>6} {'Return':>8} {'MaxDD':>8} {'Win%':>7} {'PF':>6} {'Hold':>6}"
    print(hdr)
    print("-" * 85)

    summary_df = pd.DataFrame(all_summaries)
    summary_df = summary_df.sort_values("total_return", ascending=False)
    for _, row in summary_df.iterrows():
        print(
            f"{row['label']:<48} {int(row['trade_count']):>6} "
            f"{row['total_return']:>8.0f} {row['max_drawdown']:>8.0f} "
            f"{row['win_rate']:>6.1%} {row['profit_factor']:>6.2f} "
            f"{row['avg_hold_bars']:>5.0f}b"
        )

    summary_df.to_csv(output_dir / "comparison_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\nResults saved to {output_dir}/")
    print("Done.")


if __name__ == "__main__":
    main()
