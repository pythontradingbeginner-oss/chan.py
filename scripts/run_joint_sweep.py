"""Phase C: Grade x Exit Rule joint sweep — delegated to unified backtest core.

Depends on: chan_futures.backtest.run_backtest() and chan_futures.config.StrategyConfig.
"""

from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings("ignore")

import pandas as pd

from chan_futures.config import ExitRuleSpec, StrategyConfig
from chan_futures.config_loader import load_config
from chan_futures.backtest import run_backtest

_EXIT_SPECS: dict[str, list[ExitRuleSpec]] = {
    "none": [],
    "fix180_time192": [
        ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 180.0, "grade_adjust": True}),
        ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192}),
    ],
    "fix120_trail400_40_time192": [
        ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 120.0, "grade_adjust": True}),
        ExitRuleSpec(type="TrailingStopRule", priority=30, params={"trigger_points": 400.0, "giveback_ratio": 0.4}),
        ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192}),
    ],
    "struct_fix180_trail500_50_time192": [
        ExitRuleSpec(type="StructureStopRule", priority=10, params={"grade_tighten": True}),
        ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 180.0, "grade_adjust": True}),
        ExitRuleSpec(type="TrailingStopRule", priority=30, params={"trigger_points": 500.0, "giveback_ratio": 0.5}),
        ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192}),
    ],
    "struct_fix120_trail400_40_time192": [
        ExitRuleSpec(type="StructureStopRule", priority=10, params={"grade_tighten": True}),
        ExitRuleSpec(type="FixedStopRule", priority=20, params={"stop_points": 120.0, "grade_adjust": True}),
        ExitRuleSpec(type="TrailingStopRule", priority=30, params={"trigger_points": 400.0, "giveback_ratio": 0.4}),
        ExitRuleSpec(type="TimeStopRule", priority=50, params={"max_bars": 192}),
    ],
}


def main() -> None:
    base_config = load_config(PROJECT_ROOT / "configs" / "rb_15m_trend_ideal.yaml")
    frame = pd.read_parquet(base_config.data_path)
    print(f"Data: {len(frame)} bars", flush=True)

    print()
    print("#" * 80)
    print("# Phase C: Grade x Exit Rule Joint Sweep (5,000 bars)")
    print("#" * 80)

    grades = ["ideal", "standard", "weak"]
    all_results: list[tuple[str, dict]] = []

    for grade in grades:
        for combo_name, exit_specs in _EXIT_SPECS.items():
            t0 = time.time()
            cfg = StrategyConfig(
                code=base_config.code,
                kl_type=base_config.kl_type,
                data_path=base_config.data_path,
                grading=base_config.grading.__class__(min_grade=grade),
                exits=list(exit_specs),
                execution=base_config.execution,
                risk=base_config.risk,
                sizing=base_config.sizing,
            )
            result = run_backtest(cfg, limit=5000)
            elapsed = time.time() - t0

            pnls = [t.get("pnl_points", 0) for t in result.trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            wr = len(wins) / len(pnls) if pnls else 0
            pf = sum(wins) / abs(sum(losses)) if losses else float("inf")

            rules_str = " + ".join(e.type for e in exit_specs) if exit_specs else "(none)"
            label = f"{grade}_{combo_name}"
            summary = {
                "trades": len(result.trades),
                "total_return": sum(pnls),
                "max_dd": 0.0,  # placeholder — use StandardMetrics for full breakdown
                "win_rate": wr,
                "profit_factor": pf,
                "avg_hold_bars": sum(t.get("hold_bars", 0) for t in result.trades) / max(len(result.trades), 1),
            }
            all_results.append((label, summary))
            print(f"  [{label:<42}] {elapsed:.0f}s | "
                  f"T={summary['trades']:>2} R={summary['total_return']:>7.0f} "
                  f"WR={summary['win_rate']:.1%} PF={summary['profit_factor']:.2f} "
                  f"Hold={summary['avg_hold_bars']:.0f}b", flush=True)

    # Save comparison
    rows = [{"label": label, **r} for label, r in all_results]
    df = pd.DataFrame(rows).sort_values("total_return", ascending=False)
    out = Path("reports/joint_sweep")
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "comparison.csv", index=False, encoding="utf-8-sig")

    print(f"\n{'=' * 80}")
    print("Top 5 Configurations:")
    for i, (_, row) in enumerate(df.head(5).iterrows()):
        print(f"  {i+1}. {row['label']}: "
              f"Return={row['total_return']:,.0f} "
              f"Trades={int(row['trades'])} "
              f"WR={row['win_rate']:.1%} PF={row['profit_factor']:.2f}")
    print(f"\nSaved to {out}/comparison.csv")
    print("Done.")


if __name__ == "__main__":
    main()
