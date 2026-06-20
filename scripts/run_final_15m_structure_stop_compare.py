from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_15m_sticky_exit_research import simulate_exit_strategy
from run_dc_structure_candidate_diagnostics import _candidate_features
from run_final_15m_strategy_obv_filter_compare import (
    _append_obv,
    _drawdown_rows,
    _safe_ratio,
    _summary_rows,
    _yearly_rows,
)
from run_final_15m_strategy_vnpy_backtest import (
    END,
    FINAL_CANDIDATE,
    START,
    _extract_trades,
    _restrict_range,
)


ORIGINAL_EXIT_STRATEGY = "final_15m_obv_sma40_original_exit"
STRUCTURE_STOP_STRATEGY = "final_15m_obv_sma40_structure_stop"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the final 15m OBV-filtered strategy with and without "
            "fixed DC-structure stops."
        )
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    continuous = load_continuous_1m(args.continuous_path)
    continuous = _restrict_range(continuous, start, end)
    bars_15m = aggregate_continuous_1m_to_Nm(continuous, FINAL_CANDIDATE.freq_minutes)
    bars_15m = _restrict_range(bars_15m, start, end)
    featured = _append_obv(_candidate_features(bars_15m, FINAL_CANDIDATE), window=40)

    base_long = featured["trend_macd_golden_cross"] & featured["dc_is_bull"]
    base_short = featured["trend_macd_death_cross"] & featured["dc_is_bear"]
    obv_long = base_long & featured["obv_above_sma40"]
    obv_short = base_short & featured["obv_below_sma40"]

    strategies = []
    trades = []
    signal_rows = []
    for name, enable_structure_stop in [
        (ORIGINAL_EXIT_STRATEGY, False),
        (STRUCTURE_STOP_STRATEGY, True),
    ]:
        strategy = simulate_exit_strategy(
            featured,
            long_signal=obv_long,
            short_signal=obv_short,
            strategy_name=name,
            reverse_mode="reverse",
            trail_trigger_points=800.0,
            giveback_ratio=0.5,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
            enable_structure_stop=enable_structure_stop,
        )
        trade_frame = _extract_trades(featured, strategy)
        trade_frame["strategy"] = name
        strategies.append(strategy)
        trades.append(trade_frame)
        signal_rows.append(
            {
                "strategy": name,
                "structure_stop_enabled": enable_structure_stop,
                "trend_macd_golden_cross": int(featured["trend_macd_golden_cross"].sum()),
                "trend_macd_death_cross": int(featured["trend_macd_death_cross"].sum()),
                "base_long_signals": int(base_long.sum()),
                "base_short_signals": int(base_short.sum()),
                "accepted_long_signals": int(obv_long.sum()),
                "accepted_short_signals": int(obv_short.sum()),
                "accepted_signal_ratio": _safe_ratio(
                    int(obv_long.sum() + obv_short.sum()),
                    int(base_long.sum() + base_short.sum()),
                ),
                "structure_stop_exit_count": int(
                    (strategy["event_reason"] == "structure_stop_exit").sum()
                ),
                "trail_exit_count": int((strategy["event_reason"] == "trail_exit").sum()),
            }
        )

    strategy_report = pd.concat(strategies, ignore_index=True)
    trades_report = pd.concat(trades, ignore_index=True)
    summary = _summary_rows(strategy_report, trades_report)
    yearly = _yearly_rows(strategy_report, trades_report)
    drawdowns = _drawdown_rows(strategy_report, trades_report)

    strategy_report.to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades_report.to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    drawdowns.to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(signal_rows).to_csv(
        args.reports_dir / "final_15m_structure_stop_compare_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")


if __name__ == "__main__":
    main()
