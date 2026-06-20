from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pandas as pd

from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_dc_structure_candidate_diagnostics import Candidate, _candidate_features
from run_rb_chan_bsp_filter_param_search import _append_obv_volume_windows, _signals_for_params
from run_rb_main_candidate_exit_research import (
    MAIN_EXIT_CONFIG,
    MAIN_PARAMS,
    MAIN_STRATEGY_NAME,
    VOLUME_WINDOW,
    _extract_trades,
    _load_or_build_chan_signals,
    _signal_stats,
    _summary_rows,
    _top_drawdown_rows,
    _yearly_rows,
    simulate_exit_rule,
)
from run_rb_priority_strategy_research import BASE_CANDIDATE, END, START, _restrict_range


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the upgraded RB main candidate: Chan BSP 1/1p/2 + DC30 + OBV40 + vol1 + trail_500_gb50."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports/rb_main_candidate_strategy"),
    )
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument(
        "--chan-signals-path",
        type=Path,
        default=None,
        help="Optional existing rb_15m_chan_signals.csv from the same date range.",
    )
    parser.add_argument("--zs-breakout-buffer", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=0, help="0 means full selected 15m bars.")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    featured = _build_featured_bars(args)
    chan_signals = _load_or_build_chan_signals(
        args,
        featured,
        args.reports_dir / "rb_15m_chan_signals.csv",
    )
    long_signal, short_signal = _signals_for_params(featured, chan_signals, MAIN_PARAMS)
    strategy = simulate_exit_rule(
        featured,
        long_signal=long_signal,
        short_signal=short_signal,
        strategy_name=MAIN_STRATEGY_NAME,
        config=MAIN_EXIT_CONFIG,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )
    trades = _extract_trades(featured, strategy)
    summary = _summary_rows(strategy, trades)
    yearly = _yearly_rows(strategy, trades)
    drawdowns = _top_drawdown_rows(strategy, trades)
    signal_stats = _signal_stats(featured, chan_signals, long_signal, short_signal)

    featured.to_csv(
        args.reports_dir / "rb_15m_main_candidate_features.csv",
        index=False,
        encoding="utf-8-sig",
    )
    strategy.to_csv(args.reports_dir / "main_candidate_values.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(args.reports_dir / "main_candidate_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(args.reports_dir / "main_candidate_summary.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(args.reports_dir / "main_candidate_yearly.csv", index=False, encoding="utf-8-sig")
    drawdowns.to_csv(args.reports_dir / "main_candidate_drawdowns.csv", index=False, encoding="utf-8-sig")
    signal_stats.to_csv(
        args.reports_dir / "main_candidate_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    _write_config(args.reports_dir / "main_candidate_config.json", args)

    print(f"saved reports to: {args.reports_dir}")
    print(_compact_summary(summary))


def _build_featured_bars(args: argparse.Namespace) -> pd.DataFrame:
    continuous = _restrict_range(load_continuous_1m(args.continuous_path), args.start, args.end)
    bars_15m = _restrict_range(
        aggregate_continuous_1m_to_Nm(continuous, BASE_CANDIDATE.freq_minutes),
        args.start,
        args.end,
    )
    if args.limit > 0:
        bars_15m = bars_15m.iloc[: args.limit].reset_index(drop=True)

    candidate = Candidate(
        name="main_bsp_1-1p-2_dc30_obv40_vol1",
        freq_minutes=BASE_CANDIDATE.freq_minutes,
        trend_atr_k=BASE_CANDIDATE.trend_atr_k,
        trend_atr_window=BASE_CANDIDATE.trend_atr_window,
        dc_threshold_points=MAIN_PARAMS.dc_threshold_points,
        dc_mode=BASE_CANDIDATE.dc_mode,
        dc_entry_buffer_points=BASE_CANDIDATE.dc_entry_buffer_points,
        dc_exit_buffer_points=BASE_CANDIDATE.dc_exit_buffer_points,
    )
    return _append_obv_volume_windows(
        _candidate_features(bars_15m, candidate),
        obv_window=MAIN_PARAMS.obv_window,
        volume_window=VOLUME_WINDOW,
    )


def _write_config(path: Path, args: argparse.Namespace) -> None:
    payload = {
        "strategy_name": MAIN_STRATEGY_NAME,
        "entry": {
            "bsp_types": list(MAIN_PARAMS.bsp_types),
            "dc_threshold_points": MAIN_PARAMS.dc_threshold_points,
            "obv_window": MAIN_PARAMS.obv_window,
            "volume_strength": MAIN_PARAMS.volume_strength,
            "volume_window": VOLUME_WINDOW,
            "structure_stop": MAIN_PARAMS.structure_stop,
        },
        "exit": asdict(MAIN_EXIT_CONFIG),
        "costs": {
            "fee_points": args.fee_points,
            "slippage_points": args.slippage_points,
        },
        "range": {
            "start": args.start,
            "end": args.end,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _compact_summary(summary: pd.DataFrame) -> str:
    cols = [
        "period",
        "trade_count",
        "total_return_points",
        "max_drawdown_points",
        "return_drawdown_ratio",
        "win_rate",
        "profit_factor",
    ]
    return summary[cols].to_string(index=False)


if __name__ == "__main__":
    main()
