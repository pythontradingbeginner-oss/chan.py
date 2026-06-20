from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from chan_futures.backtest import (
    ChanBacktestConfig,
    run_chan_trigger_backtest,
    save_backtest_reports,
)
from data_foundation import load_continuous_1m


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay RB continuous minute bars through chan.py trigger_load and trade latest BSP."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports/chan_rb_minute_trend"))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--limit", type=int, default=5000, help="0 means use all selected bars.")
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--long-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    frame = load_continuous_1m(args.continuous_path)
    frame = _restrict_range(frame, args.start, args.end)
    limit = None if args.limit == 0 else args.limit
    result = run_chan_trigger_backtest(
        frame,
        config=ChanBacktestConfig(
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
            allow_short=not args.long_only,
        ),
        limit=limit,
    )
    save_backtest_reports(result, args.reports_dir)
    summary = result.summary.iloc[0].to_dict()
    print(
        "processed {bar_count} bars, fills={fill_count}, "
        "return={total_return_points:.2f} points, max_drawdown={max_drawdown_points:.2f} points".format(
            **summary
        )
    )
    print(f"saved reports to: {args.reports_dir}")


def _restrict_range(frame: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start is None and end is None:
        return frame
    result = frame.copy()
    datetimes = pd.to_datetime(result["datetime"])
    if getattr(datetimes.dt, "tz", None) is not None:
        datetimes = datetimes.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    result["datetime"] = datetimes
    if start is not None:
        result = result[result["datetime"] >= pd.Timestamp(start)]
    if end is not None:
        result = result[result["datetime"] <= pd.Timestamp(end)]
    return result.reset_index(drop=True)


if __name__ == "__main__":
    main()
