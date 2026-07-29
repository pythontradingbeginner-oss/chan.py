from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_foundation import CleaningLog, generate_quality_report


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the RB quality report from built products.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    logs = sorted(args.data_dir.glob("cleaning_log_*.json"))
    if not logs:
        raise FileNotFoundError("no cleaning_log_*.json found")
    values = json.loads(logs[-1].read_text(encoding="utf-8"))
    missing = pd.read_csv(
        args.data_dir / "RB_missing_minutes.csv",
        parse_dates=["trading_day", "missing_datetime"],
    )
    summary = pd.read_csv(
        args.data_dir / "RB_contract_session_completeness.csv",
        parse_dates=["trading_day"],
    )
    out_of_session = pd.read_csv(args.data_dir / "RB_out_of_session_rows.csv")
    log = CleaningLog(
        raw_rows=values["raw_rows"],
        deduplicated=values["deduplicated"],
        invalid_price_removed=values["invalid_price_removed"],
        invalid_ohlc_removed=values["invalid_ohlc_removed"],
        extreme_moves_flagged=values["extreme_moves_flagged"],
        limit_up_flagged=values["estimated_limit_up_touches"],
        limit_down_flagged=values["estimated_limit_down_touches"],
        missing_bars_per_day={
            pd.Timestamp(key).date(): value
            for key, value in values["missing_bars_per_day"].items()
        },
        final_rows=values["final_rows"],
        affected_contract_days=values["affected_contract_days"],
        duplicate_minutes=values["duplicate_minutes"],
        out_of_session_removed=values["out_of_session_removed"],
        missing_details=missing,
        contract_day_summary=summary,
        out_of_session_details=out_of_session,
    )
    cleaned = pd.read_parquet(args.data_dir / "RB_1m_raw_clean.parquet")
    adjusted = pd.read_parquet(args.data_dir / "RB_1m_continuous_adjusted.parquet")
    switches = pd.read_csv(args.data_dir / "RB_main_switch_log.csv")
    audits = []
    for frequency in (5, 15, 30, 60):
        frame = pd.read_csv(
            args.data_dir / f"RB_{frequency}m_aggregation_audit.csv",
            parse_dates=["trading_day", "window_end"],
            low_memory=False,
        )
        audits.append(frame)
    output = args.output or args.data_dir / f"quality_report_{date.today():%Y%m%d}.html"
    generate_quality_report(
        log,
        cleaned,
        adjusted,
        switches,
        output,
        format="html",
        aggregation_audit_df=pd.concat(audits, ignore_index=True),
    )
    print(f"saved: {output}")


if __name__ == "__main__":
    main()

