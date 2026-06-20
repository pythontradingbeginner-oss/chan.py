from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_15m_sticky_exit_research import simulate_exit_strategy
from run_dc_structure_candidate_diagnostics import _candidate_features
from run_final_15m_strategy_vnpy_backtest import (
    END,
    FINAL_CANDIDATE,
    START,
    _drawdown_frame,
    _extract_trades,
    _naive_datetimes,
    _restrict_range,
    _strategy_metrics,
)


BASE_STRATEGY = "final_15m_no_obv_filter"
OBV_STRATEGY = "final_15m_obv_sma40_filter"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare final 15m strategy with and without 15m OBV SMA40 signal filter."
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
    for name, long_signal, short_signal in [
        (BASE_STRATEGY, base_long, base_short),
        (OBV_STRATEGY, obv_long, obv_short),
    ]:
        strategy = simulate_exit_strategy(
            featured,
            long_signal=long_signal,
            short_signal=short_signal,
            strategy_name=name,
            reverse_mode="reverse",
            trail_trigger_points=800.0,
            giveback_ratio=0.5,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        trade_frame = _extract_trades(featured, strategy)
        trade_frame["strategy"] = name
        strategies.append(strategy)
        trades.append(trade_frame)
        signal_rows.append(
            {
                "strategy": name,
                "trend_macd_golden_cross": int(featured["trend_macd_golden_cross"].sum()),
                "trend_macd_death_cross": int(featured["trend_macd_death_cross"].sum()),
                "base_long_signals": int(base_long.sum()),
                "base_short_signals": int(base_short.sum()),
                "accepted_long_signals": int(long_signal.sum()),
                "accepted_short_signals": int(short_signal.sum()),
                "accepted_signal_ratio": _safe_ratio(
                    int(long_signal.sum() + short_signal.sum()),
                    int(base_long.sum() + base_short.sum()),
                ),
                "obv_above_sma40_ratio": float(featured["obv_above_sma40"].mean()),
                "obv_below_sma40_ratio": float(featured["obv_below_sma40"].mean()),
            }
        )

    strategy_report = pd.concat(strategies, ignore_index=True)
    trades_report = pd.concat(trades, ignore_index=True)
    summary = _summary_rows(strategy_report, trades_report)
    yearly = _yearly_rows(strategy_report, trades_report)
    drawdowns = _drawdown_rows(strategy_report, trades_report)

    strategy_report.to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades_report.to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    drawdowns.to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(signal_rows).to_csv(
        args.reports_dir / "final_15m_obv_filter_compare_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")


def _append_obv(frame: pd.DataFrame, *, window: int) -> pd.DataFrame:
    if window <= 0:
        raise ValueError("window must be positive")
    result = frame.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    volume = pd.to_numeric(result["volume"], errors="coerce").fillna(0.0)
    direction = np.sign(close.diff()).fillna(0.0)
    result["obv"] = (direction * volume).cumsum()
    result["obv_sma40"] = result["obv"].rolling(window, min_periods=window).mean()
    result["obv_above_sma40"] = result["obv"] > result["obv_sma40"]
    result["obv_below_sma40"] = result["obv"] < result["obv_sma40"]
    return result


def _summary_rows(strategy_report: pd.DataFrame, trades_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        trades = trades_report[trades_report["strategy"] == strategy_name]
        rows.append(
            {
                "strategy": strategy_name,
                "period": "full_2018-2025YTD",
                **_strategy_metrics(strategy, trades),
            }
        )
    return pd.DataFrame(rows)


def _yearly_rows(strategy_report: pd.DataFrame, trades_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        strategy_with_year = strategy.copy()
        strategy_with_year["year"] = _naive_datetimes(strategy_with_year["datetime"]).dt.year
        trades = trades_report[trades_report["strategy"] == strategy_name].copy()
        if not trades.empty:
            trades["year"] = _naive_datetimes(trades["entry_time"]).dt.year
        for year, year_strategy in strategy_with_year.groupby("year", sort=True):
            year_trades = trades[trades["year"] == year] if not trades.empty else pd.DataFrame()
            rows.append(
                {
                    "strategy": strategy_name,
                    "year": int(year),
                    **_strategy_metrics(year_strategy, year_trades),
                }
            )
    return pd.DataFrame(rows)


def _drawdown_rows(strategy_report: pd.DataFrame, trades_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        drawdowns = _drawdown_frame(
            strategy.reset_index(drop=True),
            trades_report[trades_report["strategy"] == strategy_name],
        )
        drawdowns.insert(0, "strategy", strategy_name)
        rows.append(drawdowns)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _safe_ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


if __name__ == "__main__":
    main()
