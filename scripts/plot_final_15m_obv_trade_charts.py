from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from data_foundation import (
    aggregate_continuous_1m_to_Nm,
    load_continuous_1m,
    plot_strategy_trade_year_charts,
)
from run_dc_structure_candidate_diagnostics import _candidate_features
from run_final_15m_strategy_obv_filter_compare import OBV_STRATEGY, _append_obv
from run_final_15m_strategy_vnpy_backtest import END, FINAL_CANDIDATE, START, _restrict_range


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot yearly 15m trade charts for the final OBV-filtered strategy."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument(
        "--trades-path",
        type=Path,
        default=Path("reports/final_15m_obv_filter_compare_trades.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/final_15m_obv_sma40_trade_charts"),
    )
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--strategy-name", default=OBV_STRATEGY)
    parser.add_argument("--obv-window", type=int, default=40)
    parser.add_argument(
        "--filename-prefix",
        default="final_15m_obv_sma40_trade_chart",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    continuous = load_continuous_1m(args.continuous_path)
    continuous = _restrict_range(continuous, start, end)
    bars_15m = aggregate_continuous_1m_to_Nm(continuous, FINAL_CANDIDATE.freq_minutes)
    bars_15m = _restrict_range(bars_15m, start, end)
    featured = _append_obv(
        _candidate_features(bars_15m, FINAL_CANDIDATE),
        window=args.obv_window,
    )

    trades = pd.read_csv(args.trades_path, encoding="utf-8-sig")
    selected_trade_count = int((trades["strategy"] == args.strategy_name).sum())
    paths = plot_strategy_trade_year_charts(
        featured,
        trades,
        args.output_dir,
        strategy_name=args.strategy_name,
        filename_prefix=args.filename_prefix,
    )

    print(f"saved {len(paths)} yearly chart(s) to: {args.output_dir}")
    print(f"strategy: {args.strategy_name}")
    print(f"entry markers: {selected_trade_count}")
    print(f"exit markers: {selected_trade_count}")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
