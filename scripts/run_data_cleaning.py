from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from data_foundation import clean_rb_1m_bars, generate_quality_report, load_raw_from_vnpy


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Clean RB one-minute vn.py data.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(r"C:\Users\Administrator\.vntrader\database.db"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--start", type=datetime.fromisoformat)
    parser.add_argument("--end", type=datetime.fromisoformat)
    parser.add_argument("--report", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run raw loading, cleaning, persistence, and optional reporting."""
    args = parse_arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    raw = load_raw_from_vnpy(args.db_path, start=args.start, end=args.end)
    cleaned, log = clean_rb_1m_bars(raw)

    cleaned_path = args.output_dir / "RB_1m_raw_clean.parquet"
    cleaned.to_parquet(cleaned_path, compression="snappy", index=False)

    stamp = date.today().strftime("%Y%m%d")
    log_path = args.output_dir / f"cleaning_log_{stamp}.json"
    log_path.write_text(
        json.dumps(log.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.report:
        continuous_path = args.output_dir / "RB_1m_continuous.parquet"
        switch_log_path = args.output_dir / "RB_main_switch_log.csv"
        continuous = (
            pd.read_parquet(continuous_path)
            if continuous_path.exists()
            else cleaned.iloc[0:0].copy()
        )
        switch_log = (
            pd.read_csv(switch_log_path)
            if switch_log_path.exists()
            else cleaned.iloc[0:0].copy()
        )
        generate_quality_report(
            log,
            cleaned,
            continuous,
            switch_log,
            args.output_dir / f"quality_report_{stamp}.html",
            format="html",
        )

    print(f"raw rows: {log.raw_rows}")
    print(f"final rows: {log.final_rows}")
    print(f"duplicates removed: {log.deduplicated}")
    print(f"invalid price removed: {log.invalid_price_removed}")
    print(f"invalid OHLC removed: {log.invalid_ohlc_removed}")
    print(f"extreme moves flagged: {log.extreme_moves_flagged}")
    print(f"saved: {cleaned_path}")


if __name__ == "__main__":
    main()
