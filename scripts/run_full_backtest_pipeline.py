"""Phase C & D: Full backtest with standardized reporting + walk-forward analysis.

Combined pipeline:
  1. Full backtest (all 37,860 bars) with the best strategy config
  2. Year-by-year segmented backtest (walk-forward)
  3. Main-contract segment testing
  4. Cost pressure testing (higher fees/slippage)
  5. Entry + Exit joint sweep

Output: reports/full_backtest_* / reports/walkforward_* / reports/cost_stress_*
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
    ExitManager, ExitRule, ExitSignal,
    StructureStopRule, FixedStopRule, TrailingStopRule, TimeStopRule,
)
from strategy_policy.reporting import (
    StandardMetrics, compute_standard_metrics, save_standard_report,
    save_comparison_report,
)


# ════════════════════════════════════════════════════════════════
# Core backtest engine (portable)
# ════════════════════════════════════════════════════════════════

_TYPE_STR_TO_BSP = {
    "1": BSP_TYPE.T1, "1p": BSP_TYPE.T1P,
    "2": BSP_TYPE.T2, "2s": BSP_TYPE.T2S,
    "3a": BSP_TYPE.T3A, "3b": BSP_TYPE.T3B,
}


def _new_chan(kl_type: KL_TYPE = KL_TYPE.K_15M, code: str = "RB_MAIN") -> CChan:
    return CChan(
        code=code, begin_time=None, end_time=None,
        data_src=DATA_SRC.CSV, lv_list=[kl_type],
        config=CChanConfig({
            "trigger_step": True, "bi_strict": True,
            "divergence_rate": float("inf"),
            "bsp2_follow_1": False, "bsp3_follow_1": False,
            "min_zs_cnt": 0, "bs1_peak": False,
            "macd_algo": "peak", "bs_type": "1,1p,2,2s,3a,3b",
            "print_warning": False,
        }),
        autype=AUTYPE.NONE,
    )


def run_full_backtest(
    frame: pd.DataFrame,
    *,
    min_grade: str = "ideal",
    accepted_bsp_types: tuple[str, ...] | None = None,
    exit_rules: list[ExitRule] | None = None,
    fee_points: float = 1.0,
    slippage_points: float = 1.0,
    allow_short: bool = True,
    kl_type: KL_TYPE = KL_TYPE.K_15M,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict], pd.DataFrame, ExitManager]:
    """Run a complete backtest and return all artifacts.

    Returns: (bars_df, fills_df, trades_list, exit_events_df, exit_manager)
    """
    bars = prepare_ohlc_frame(frame)
    chan = _new_chan(kl_type=kl_type)

    # Grade filter
    accepted_bsp = None
    if accepted_bsp_types is not None:
        accepted_bsp = [_TYPE_STR_TO_BSP[t] for t in accepted_bsp_types if t in _TYPE_STR_TO_BSP]

    wrapper = GradedChanStrategy(GradeFilterConfig(
        min_grade=min_grade, accepted_bsp_types=accepted_bsp,
        allow_short=allow_short,
    ))
    timeframe_str = kl_type.name.replace("K_", "").replace("M", "m")
    extractor = SignalExtractor(symbol="RB", timeframe=timeframe_str)

    risk = RiskManager(RiskConfig(max_abs_position=1, max_loss_points=None))
    execution = SimulatedExecutionEngine(
        fee_points=fee_points, slippage_points=slippage_points,
    )
    exit_manager = ExitManager(exit_rules or [], strict=False)
    use_exit_rules = exit_rules is not None and len(exit_rules) > 0

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
        pnl -= fee_points * 2  # entry + exit fees
        current_trade.update({
            "exit_bar": exit_bar, "exit_price": exit_price,
            "exit_time": exit_time,
            "hold_bars": exit_bar - current_trade["entry_bar"],
            "pnl_points": round(pnl, 1),
            "exit_reason": reason, "exit_rule": rule_id,
        })
        trades.append(current_trade)
        current_trade = None

    for row_number, row in bars.iterrows():
        klu = row_to_klu(row, kl_type=kl_type)
        chan.trigger_load({kl_type: [klu]})
        price = float(row["close"])
        timestamp = row["datetime"]
        if isinstance(timestamp, pd.Timestamp):
            dt_timestamp = timestamp.to_pydatetime()
        else:
            dt_timestamp = timestamp
        active_symbol = row.get("active_symbol")

        # ── 1. Check exit rules ──
        exit_signal: ExitSignal | None = None
        if use_exit_rules and exit_manager.is_active:
            exit_signal = exit_manager.check(
                bar_end_time=dt_timestamp, open=float(row["open"]),
                high=float(row["high"]), low=float(row["low"]), close=price,
            )
            if exit_signal is not None and execution.state.position != 0:
                _close_trade(
                    exit_bar=row_number, exit_price=exit_signal.exit_price,
                    exit_time=timestamp, reason=exit_signal.reason_code,
                    rule_id=exit_signal.rule_id,
                )
                # Force close
                from chan_futures.strategy import StrategySignal
                target = 0
                action = "close_long" if execution.state.position > 0 else "close_short"
                sig = StrategySignal(
                    timestamp=timestamp, action=action, target_position=0,
                    price=price, reason=exit_signal.reason_code,
                    bsp_type="exit", bsp_bi_idx=-1, bsp_klu_idx=-1,
                    active_symbol=active_symbol,
                )
                execution.execute(sig)
                exit_manager.on_close()
                exit_events.append({
                    "row_number": row_number, "datetime": timestamp,
                    "rule_id": exit_signal.rule_id,
                    "reason_code": exit_signal.reason_code,
                    "trigger_price": exit_signal.exit_price,
                    "description": exit_signal.description,
                })

        # ── 2. Check entries ──
        if exit_signal is None:
            graded = wrapper.on_bar(
                chan=chan, current_position=execution.state.position,
                price=price, timestamp=timestamp,
                active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
                lv_idx=0, extractor=extractor,
            )
            signal = graded.signal if graded is not None else None
            grade_info = {"grade": graded.grade} if graded else None

            if signal is not None:
                has_pos = execution.state.position != 0
                if has_pos and signal.action in (
                    "close_long", "close_short",
                    "reverse_long_to_short", "reverse_short_to_long",
                ):
                    _close_trade(
                        exit_bar=row_number, exit_price=price,
                        exit_time=timestamp, reason="strategy_reverse",
                        rule_id="strategy_reversal",
                    )
                    exit_manager.on_close()

                decision = risk.approve(signal, realized_points=execution.state.realized_points)
                if decision.approved:
                    fill = execution.execute(signal)
                    if fill is not None and fill.target_position != 0:
                        direction = "long" if fill.target_position > 0 else "short"
                        current_trade = {
                            "entry_bar": row_number,
                            "entry_price": fill.fill_price - fee_points,
                            "entry_time": timestamp,
                            "direction": direction,
                            "grade": grade_info["grade"] if grade_info else "unknown",
                            "active_symbol": str(active_symbol) if pd.notna(active_symbol) else "",
                        }
                        dir_enum = SignalDirection.LONG if direction == "long" else SignalDirection.SHORT
                        eg = grade_info["grade"] if grade_info else None
                        exit_manager.on_entry(
                            direction=dir_enum, entry_price=fill.fill_price,
                            entry_grade=eg,
                        )

        records.append({
            "row_number": row_number, "datetime": timestamp,
            "close": price, "position": execution.state.position,
            "avg_price": execution.state.avg_price,
            "realized_points": execution.state.realized_points,
            "equity_points": execution.mark_to_market(price),
            "exit_reason": exit_signal.reason_code if exit_signal else None,
        })

    # Close open trade at end
    if current_trade is not None and execution.state.position != 0:
        last_bar = bars.iloc[-1]
        _close_trade(
            exit_bar=len(bars) - 1, exit_price=float(last_bar["close"]),
            exit_time=last_bar["datetime"], reason="end_of_data",
            rule_id="end_of_data",
        )

    equity = pd.DataFrame(records)
    fills = pd.DataFrame([f.__dict__ for f in execution.fills])
    exit_evts = pd.DataFrame(exit_events)
    return equity, fills, trades, exit_evts, exit_manager


# ════════════════════════════════════════════════════════════════
# Phase C: Entry + Exit joint sweep
# ════════════════════════════════════════════════════════════════

def phase_c_joint_sweep(
    frame: pd.DataFrame,
    *,
    limit: int | None = 5000,
) -> None:
    """Sweep grade levels and exit rule combinations."""
    print("\n" + "=" * 70)
    print("Phase C: Entry + Exit Joint Optimization")
    print("=" * 70)

    output_dir = Path("reports/joint_sweep")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fixed entry: ideal T1+T1P, sweep exit combos
    grades = ["ideal", "standard", "weak"]
    exit_sets: list[tuple[str, list[ExitRule]]] = [
        ("strategy_only", []),
        ("fix180_time192", [
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TimeStopRule(max_bars=192),
        ]),
        ("fix120_trail400_40_time192", [
            FixedStopRule(stop_points=120.0, grade_adjust=True),
            TrailingStopRule(trigger_points=400.0, giveback_ratio=0.4),
            TimeStopRule(max_bars=192),
        ]),
        ("struct_fix180_trail500_50_time192", [
            StructureStopRule(grade_tighten=True),
            FixedStopRule(stop_points=180.0, grade_adjust=True),
            TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
            TimeStopRule(max_bars=192),
        ]),
    ]

    all_metrics: list[tuple[str, StandardMetrics]] = []

    for grade in grades:
        for exit_name, exit_rules in exit_sets:
            label = f"{grade}_{exit_name}"
            rules_str = " + ".join(r.rule_id for r in exit_rules) if exit_rules else "(none)"

            t0 = time.time()
            equity, fills, trades, exit_evts, exit_mgr = run_full_backtest(
                frame, min_grade=grade, exit_rules=exit_rules,
            )
            elapsed = time.time() - t0

            metrics = compute_standard_metrics(
                equity, fills, trades, exit_evts,
                strategy_name="joint_sweep", variant=label,
                timeframe_minutes=15,
            )

            save_standard_report(
                output_dir / label,
                bars=equity, fills=fills, trades=trades,
                exit_events=exit_evts,
                config={"min_grade": grade, "exit_rules": rules_str,
                       "fee_points": 1.0, "slippage_points": 1.0,
                       "limit_bars": limit},
                metrics=metrics,
            )

            print(f"  [{label:<35}] {elapsed:.0f}s | "
                  f"Trades={metrics.trade_count} Return={metrics.total_return_points:,.0f}pts "
                  f"DD={metrics.max_drawdown_points:,.0f}pts WR={metrics.win_rate:.1%} "
                  f"PF={metrics.profit_factor:.2f}")

            all_metrics.append((label, metrics))

    # Save comparison
    comp_df = save_comparison_report(
        output_dir, all_metrics, sort_by="total_return_points",
    )
    print(f"\nComparison saved to {output_dir}/comparison.csv")

    # Print top performers
    print(f"\n{'─' * 70}")
    print("Top 5 Configurations:")
    top = [(l, m) for l, m in all_metrics]
    top.sort(key=lambda x: -x[1].total_return_points)
    for i, (l, m) in enumerate(top[:5]):
        print(f"  {i+1}. {l}: Return={m.total_return_points:,.0f}pts "
              f"DD={m.max_drawdown_points:,.0f} WR={m.win_rate:.1%} "
              f"PF={m.profit_factor:.2f} Trades={m.trade_count}")


# ════════════════════════════════════════════════════════════════
# Phase D: Walk-forward + contract segmentation
# ════════════════════════════════════════════════════════════════

def phase_d_walkforward(
    frame: pd.DataFrame,
    *,
    limit: int | None = None,
) -> None:
    """Walk-forward analysis: year-by-year + contract-segment + cost stress."""
    print("\n" + "=" * 70)
    print("Phase D: Walk-forward Robustness Testing")
    print("=" * 70)

    output_dir = Path("reports/walkforward")
    output_dir.mkdir(parents=True, exist_ok=True)

    bars = prepare_ohlc_frame(frame)
    if limit is not None and limit > 0:
        bars = bars.iloc[:limit].reset_index(drop=True)

    full_range = bars["datetime"]
    min_dt = pd.Timestamp(full_range.min())
    max_dt = pd.Timestamp(full_range.max())
    print(f"Full range: {min_dt} → {max_dt}")

    # ── D.1: Year-by-year segmentation ──
    print(f"\n{'─' * 50}")
    print("D.1 Year-by-year Performance")
    print(f"{'─' * 50}")

    yearly_metrics: list[tuple[str, StandardMetrics]] = []
    # Determine year boundaries
    years = range(2018, 2026)
    for year in years:
        start = pd.Timestamp(f"{year}-01-01", tz=min_dt.tz)
        end = pd.Timestamp(f"{year+1}-01-01", tz=min_dt.tz)
        year_bars = bars[(bars["datetime"] >= start) & (bars["datetime"] < end)].copy()
        if len(year_bars) < 100:
            print(f"  {year}: {len(year_bars)} bars — skipping (too few)")
            continue

        t0 = time.time()
        equity, fills, trades, exit_evts, _ = run_full_backtest(
            year_bars.reset_index(drop=True),
            min_grade="ideal",
        )
        elapsed = time.time() - t0

        metrics = compute_standard_metrics(
            equity, fills, trades, exit_evts,
            strategy_name="walkforward", variant=str(year),
            timeframe_minutes=15,
        )
        save_standard_report(
            output_dir / f"year_{year}",
            bars=equity, fills=fills, trades=trades,
            exit_events=exit_evts,
            config={"year": year, "bars": len(year_bars),
                   "min_grade": "ideal"},
            metrics=metrics,
        )
        print(f"  {year}: {metrics.trade_count} trades "
              f"Return={metrics.total_return_points:,.0f}pts "
              f"DD={metrics.max_drawdown_points:,.0f} "
              f"WR={metrics.win_rate:.1%} PF={metrics.profit_factor:.2f}")

        yearly_metrics.append((str(year), metrics))

    save_comparison_report(output_dir, yearly_metrics, sort_by="total_return_points")

    # ── D.2: Main-contract segmentation ──
    print(f"\n{'─' * 50}")
    print("D.2 Main-contract Segment Performance")
    print(f"{'─' * 50}")

    if "active_symbol" in bars.columns:
        contract_metrics: list[tuple[str, StandardMetrics]] = []
        contracts = bars["active_symbol"].dropna().unique()
        for contract in contracts:
            c_bars = bars[bars["active_symbol"] == contract].copy()
            if len(c_bars) < 50:
                continue
            t0 = time.time()
            equity, fills, trades, exit_evts, _ = run_full_backtest(
                c_bars.reset_index(drop=True), min_grade="ideal",
            )
            elapsed = time.time() - t0
            metrics = compute_standard_metrics(
                equity, fills, trades, exit_evts,
                strategy_name="contract_segment", variant=str(contract),
                timeframe_minutes=15,
            )
            # Only save if there are trades
            if metrics.trade_count > 0:
                save_standard_report(
                    output_dir / f"contract_{contract}",
                    bars=equity, fills=fills, trades=trades,
                    exit_events=exit_evts,
                    config={"contract": str(contract), "bars": len(c_bars),
                           "min_grade": "ideal"},
                    metrics=metrics,
                )
                print(f"  {contract}: {metrics.trade_count} trades "
                      f"Return={metrics.total_return_points:,.0f}pts "
                      f"DD={metrics.max_drawdown_points:,.0f} "
                      f"WR={metrics.win_rate:.1%} PF={metrics.profit_factor:.2f}")
                contract_metrics.append((str(contract), metrics))

        if contract_metrics:
            save_comparison_report(output_dir / "contracts",
                                  contract_metrics, sort_by="total_return_points")
    else:
        print("  No active_symbol column found — skipping contract segmentation.")

    # ── D.3: Cost pressure testing ──
    print(f"\n{'─' * 50}")
    print("D.3 Cost Pressure Testing")
    print(f"{'─' * 50}")

    cost_configs = [
        ("baseline", 1.0, 1.0),
        ("fee2x", 2.0, 1.0),
        ("slippage2x", 1.0, 2.0),
        ("cost2x", 2.0, 2.0),
        ("fee3_slip2", 3.0, 2.0),
        ("worst_case", 3.0, 3.0),
    ]

    cost_metrics: list[tuple[str, StandardMetrics]] = []
    for cost_label, fee, slip in cost_configs:
        t0 = time.time()
        equity, fills, trades, exit_evts, _ = run_full_backtest(
            bars, min_grade="ideal",
            fee_points=fee, slippage_points=slip,
        )
        elapsed = time.time() - t0
        metrics = compute_standard_metrics(
            equity, fills, trades, exit_evts,
            strategy_name="cost_stress", variant=cost_label,
            timeframe_minutes=15,
        )
        save_standard_report(
            output_dir / f"cost_{cost_label}",
            bars=equity, fills=fills, trades=trades,
            exit_events=exit_evts,
            config={"fee_points": fee, "slippage_points": slip,
                   "min_grade": "ideal"},
            metrics=metrics,
        )
        print(f"  {cost_label} (fee={fee}, slip={slip}): "
              f"Return={metrics.total_return_points:,.0f}pts "
              f"DD={metrics.max_drawdown_points:,.0f} "
              f"Trades={metrics.trade_count} "
              f"PF={metrics.profit_factor:.2f}")
        cost_metrics.append((cost_label, metrics))

    save_comparison_report(output_dir / "cost_stress",
                          cost_metrics, sort_by="total_return_points")

    print(f"\nAll walk-forward results saved to {output_dir}/")
    print("Done.")


# ════════════════════════════════════════════════════════════════
# Phase B: Full backtest with standardized report
# ════════════════════════════════════════════════════════════════

def phase_b_full_backtest(
    frame: pd.DataFrame,
    *,
    limit: int | None = None,
    min_grade: str = "ideal",
    exit_rules: list[ExitRule] | None = None,
    output_dir: Path = Path("reports/full_backtest_ideal"),
    strategy_name: str = "chan_bsp_trend",
    variant: str = "ideal_baseline",
) -> StandardMetrics:
    """Run a full backtest and save standardized reports."""
    print("\n" + "=" * 70)
    print("Phase B: Full Backtest — Standardized Report")
    print("=" * 70)

    bars = prepare_ohlc_frame(frame)
    if limit is not None and limit > 0:
        bars = bars.iloc[:limit].reset_index(drop=True)
    print(f"Data: {len(bars)} bars")

    t0 = time.time()
    equity, fills, trades, exit_evts, exit_mgr = run_full_backtest(
        bars, min_grade=min_grade, exit_rules=exit_rules,
    )
    elapsed = time.time() - t0

    rules_str = " + ".join(r.rule_id for r in (exit_rules or [])) if exit_rules else "(strategy reversal only)"

    metrics = compute_standard_metrics(
        equity, fills, trades, exit_evts,
        strategy_name=strategy_name, variant=variant,
        timeframe_minutes=15,
    )

    config = {
        "min_grade": min_grade,
        "exit_rules": rules_str,
        "fee_points": 1.0, "slippage_points": 1.0,
        "allow_short": True,
        "bar_count": len(bars),
    }

    save_standard_report(
        output_dir,
        bars=equity, fills=fills, trades=trades,
        exit_events=exit_evts,
        config=config, metrics=metrics,
    )

    print(f"Complete in {elapsed:.1f}s — {metrics.trade_count} trades")
    metrics.print_summary()

    return metrics


# ════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════

def main() -> None:
    """Full pipeline using the unified backtest core."""
    from chan_futures.config_loader import load_config
    from chan_futures.backtest import run_backtest

    config = load_config(PROJECT_ROOT / "configs" / "rb_15m_trend_ideal.yaml")

    print(f"Config: {config.code} {config.kl_type} min_grade={config.grading.min_grade.value}")
    print(f"Exit rules: {[e.type for e in config.exits]}")

    frame = pd.read_parquet(config.data_path)
    print(f"Loaded RB 15m: {len(frame)} bars")

    # ── Phase B: Full backtest (all bars) ──
    print("\n" + "#" * 70)
    print("# Phase B: Full Backtest (37,860 bars)")
    print("#" * 70)
    t0 = time.time()
    result = run_backtest(config, limit=None)
    elapsed = time.time() - t0
    result.save(Path("reports/full_backtest_ideal"))

    # Print summary
    total_pnl = sum(t.get("pnl_points", 0) for t in result.trades)
    wins = [t for t in result.trades if t.get("pnl_points", 0) > 0]
    print(f"Complete in {elapsed:.0f}s — {len(result.trades)} trades")
    print(f"Total PnL: {total_pnl:.0f} pts | Win rate: {len(wins)/max(len(result.trades),1):.1%}")
    print(f"Exit manager errors: {result.exit_manager.error_summary()}")

    # ── Phase C: Joint sweep (5000 bars for speed) ──
    print("\n" + "#" * 70)
    print("# Phase C: Entry + Exit Joint Sweep (5,000 bars)")
    print("#" * 70)
    _phase_c_sweep(frame)

    # ── Phase D: Walk-forward (5000 bars for speed) ──
    print("\n" + "#" * 70)
    print("# Phase D: Walk-forward Analysis (5,000 bars)")
    print("#" * 70)
    _phase_d_walkforward(frame)

    print("\n" + "=" * 70)
    print("All Phase B/C/D complete!")
    print("=" * 70)


def _phase_c_sweep(frame: pd.DataFrame) -> None:
    """Joint sweep over grade × exit combos, using unified backtest core."""
    from chan_futures.config import StrategyConfig, ExitRuleSpec
    from chan_futures.backtest import run_backtest
    from strategy_policy.exit_rules import StructureStopRule, FixedStopRule, TrailingStopRule, TimeStopRule
    from strategy_policy.reporting import compute_standard_metrics, save_standard_report, save_comparison_report

    bars_5k = prepare_ohlc_frame(frame).iloc[:5000].reset_index(drop=True)
    grades = ["ideal", "standard", "weak"]
    exit_sets: list[tuple[str, list[ExitRuleSpec]]] = [
        ("strategy_only", []),
        ("fix180_time192", [
            ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 180.0, "grade_adjust": True}),
            ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192})]),
        ("fix120_trail400_40_time192", [
            ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 120.0, "grade_adjust": True}),
            ExitRuleSpec(type="TrailingStopRule", priority=30, params={"trigger_points": 400.0, "giveback_ratio": 0.4}),
            ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192})]),
        ("struct_fix180_trail500_50_time192", [
            ExitRuleSpec(type="StructureStopRule", priority=10, params={"grade_tighten": True}),
            ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 180.0, "grade_adjust": True}),
            ExitRuleSpec(type="TrailingStopRule", priority=30, params={"trigger_points": 500.0, "giveback_ratio": 0.5}),
            ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192})]),
    ]

    output_dir = Path("reports/joint_sweep")
    output_dir.mkdir(parents=True, exist_ok=True)
    all_metrics: list[tuple[str, object]] = []

    from chan_futures.config_loader import load_config
    base_config = load_config(PROJECT_ROOT / "configs" / "rb_15m_trend_ideal.yaml")

    for grade in grades:
        for exit_name, exit_specs in exit_sets:
            label = f"{grade}_{exit_name}"
            from chan_futures.config import ScoreGradeStr
            cfg = StrategyConfig(
                code=base_config.code, kl_type=base_config.kl_type,
                grading=base_config.grading.__class__(min_grade=ScoreGradeStr(grade)),
                exits=list(exit_specs),
                execution=base_config.execution,
                risk=base_config.risk,
                sizing=base_config.sizing,
            )

            t0 = time.time()
            result = run_backtest(cfg, limit=5000)
            elapsed = time.time() - t0

            metrics = compute_standard_metrics(
                result.bars, result.fills, result.trades, result.exit_events,
                strategy_name="joint_sweep", variant=label, timeframe_minutes=15,
            )
            save_standard_report(output_dir / label,
                bars=result.bars, fills=result.fills, trades=result.trades,
                exit_events=result.exit_events,
                config={"min_grade": grade, "exit_rules": exit_name, "limit_bars": 5000},
                metrics=metrics,
            )
            rules_str = " + ".join(e.type for e in exit_specs) if exit_specs else "(none)"
            print(f"  [{label:<35}] {elapsed:.0f}s | Trades={metrics.trade_count} Return={metrics.total_return_points:,.0f}pts DD={metrics.max_drawdown_points:,.0f}pts WR={metrics.win_rate:.1%} PF={metrics.profit_factor:.2f}")
            all_metrics.append((label, metrics))

    comp_df = save_comparison_report(output_dir, all_metrics, sort_by="total_return_points")
    print(f"\nComparison saved to {output_dir}/comparison.csv")


def _phase_d_walkforward(frame: pd.DataFrame) -> None:
    """Walk-forward analysis via unified backtest core."""
    from chan_futures.config_loader import load_config
    from chan_futures.backtest import run_backtest
    from strategy_policy.reporting import compute_standard_metrics, save_standard_report, save_comparison_report

    config = load_config(PROJECT_ROOT / "configs" / "rb_15m_trend_ideal.yaml")
    bars = prepare_ohlc_frame(frame)
    full_range = bars["datetime"]
    min_dt = pd.Timestamp(full_range.min())
    max_dt = pd.Timestamp(full_range.max())
    print(f"Full range: {min_dt} → {max_dt}")

    output_dir = Path("reports/walkforward")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Year-by-year
    yearly_metrics: list[tuple[str, object]] = []
    for year in range(2018, 2026):
        start = pd.Timestamp(f"{year}-01-01", tz=min_dt.tz)
        end = pd.Timestamp(f"{year+1}-01-01", tz=min_dt.tz)
        year_bars = bars[(bars["datetime"] >= start) & (bars["datetime"] < end)].copy()
        if len(year_bars) < 100:
            continue

        t0 = time.time()
        result = run_backtest(config, frame=year_bars)
        elapsed = time.time() - t0
        metrics = compute_standard_metrics(
            result.bars, result.fills, result.trades, result.exit_events,
            strategy_name="walkforward", variant=str(year), timeframe_minutes=15,
        )
        save_standard_report(output_dir / f"year_{year}",
            bars=result.bars, fills=result.fills, trades=result.trades,
            exit_events=result.exit_events,
            config={"year": year, "bars": len(year_bars), "min_grade": config.grading.min_grade.value},
            metrics=metrics,
        )
        print(f"  {year}: {metrics.trade_count} trades Return={metrics.total_return_points:,.0f}pts DD={metrics.max_drawdown_points:,.0f} WR={metrics.win_rate:.1%} PF={metrics.profit_factor:.2f}")
        yearly_metrics.append((str(year), metrics))

    save_comparison_report(output_dir, yearly_metrics, sort_by="total_return_points")
    print(f"\nWalk-forward saved to {output_dir}/")


if __name__ == "__main__":
    main()
