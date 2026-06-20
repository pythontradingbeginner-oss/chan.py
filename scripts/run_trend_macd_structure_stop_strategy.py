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
    _drawdown_rows,
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


STRATEGY_NAME = "trend_macd_structure_stop"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest pure trend-MACD reversal entries with fixed DC-structure stops."
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
    featured = _candidate_features(bars_15m, FINAL_CANDIDATE)

    long_signal = featured["trend_macd_golden_cross"]
    short_signal = featured["trend_macd_death_cross"]
    strategy = simulate_exit_strategy(
        featured,
        long_signal=long_signal,
        short_signal=short_signal,
        strategy_name=STRATEGY_NAME,
        reverse_mode="reverse",
        trail_trigger_points=None,
        giveback_ratio=None,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
        enable_structure_stop=True,
    )
    trades = _extract_trades(featured, strategy)
    trades = _ensure_trade_columns(trades)

    summary = _summary_rows(strategy, trades)
    yearly = _yearly_rows(strategy, trades)
    drawdowns = _drawdown_rows(strategy, trades)
    signal_stats = _signal_stats(
        featured=featured,
        strategy=strategy,
        long_signal=long_signal,
        short_signal=short_signal,
        bars_15m=bars_15m,
        continuous=continuous,
        start=start,
        end=end,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )

    strategy.to_csv(
        args.reports_dir / "trend_macd_structure_stop_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades.to_csv(
        args.reports_dir / "trend_macd_structure_stop_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "trend_macd_structure_stop_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "trend_macd_structure_stop_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    drawdowns.to_csv(
        args.reports_dir / "trend_macd_structure_stop_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    signal_stats.to_csv(
        args.reports_dir / "trend_macd_structure_stop_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")


def _signal_stats(
    *,
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    long_signal: pd.Series,
    short_signal: pd.Series,
    bars_15m: pd.DataFrame,
    continuous: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    rows = [
        {
            "strategy": STRATEGY_NAME,
            "requested_start": start,
            "requested_end": end,
            "continuous_rows": float(len(continuous)),
            "bars_15m": float(len(bars_15m)),
            "featured_rows": float(len(featured)),
            "freq_minutes": FINAL_CANDIDATE.freq_minutes,
            "trend_atr_k": FINAL_CANDIDATE.trend_atr_k,
            "trend_atr_window": FINAL_CANDIDATE.trend_atr_window,
            "dc_threshold_points": FINAL_CANDIDATE.dc_threshold_points,
            "dc_mode": FINAL_CANDIDATE.dc_mode,
            "dc_entry_buffer_points": FINAL_CANDIDATE.dc_entry_buffer_points,
            "dc_exit_buffer_points": FINAL_CANDIDATE.dc_exit_buffer_points,
            "reverse_mode": "reverse",
            "trail_trigger_points": None,
            "giveback_ratio": None,
            "structure_stop_enabled": True,
            "fee_points": fee_points,
            "slippage_points": slippage_points,
            "trend_macd_golden_cross": int(featured["trend_macd_golden_cross"].sum()),
            "trend_macd_death_cross": int(featured["trend_macd_death_cross"].sum()),
            "accepted_long_signals": int(long_signal.sum()),
            "accepted_short_signals": int(short_signal.sum()),
            "simultaneous_signal_count": int((long_signal & short_signal).sum()),
            "structure_stop_exit_count": int(
                (strategy["event_reason"] == "structure_stop_exit").sum()
            ),
            "long_signal_count": int((strategy["event_reason"] == "long_signal").sum()),
            "short_signal_count": int((strategy["event_reason"] == "short_signal").sum()),
            "trail_exit_count": int((strategy["event_reason"] == "trail_exit").sum()),
        }
    ]
    return pd.DataFrame(rows)


def _ensure_trade_columns(trades: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    for column in ["entry_row", "exit_row", "net_return_points"]:
        if column not in result:
            result[column] = pd.Series(dtype="float64")
    result["strategy"] = STRATEGY_NAME
    return result


if __name__ == "__main__":
    main()
