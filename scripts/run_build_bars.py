from __future__ import annotations

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_foundation import RBTradingCalendar, aggregate_continuous_1m_to_Nm, load_continuous_1m


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build session-aligned RB bars.")
    parser.add_argument("--input", type=Path, default=Path("data/processed/RB_1m_continuous_raw.parquet"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = load_continuous_1m("raw", path=args.input)
    calendar = RBTradingCalendar.load_default()
    for frequency in (5, 15, 30, 60):
        bars, audit = aggregate_continuous_1m_to_Nm(
            source, frequency, calendar=calendar, return_audit=True
        )
        bars_path = args.output_dir / f"RB_{frequency}m_continuous_raw.parquet"
        audit_path = args.output_dir / f"RB_{frequency}m_aggregation_audit.csv"
        bars.to_parquet(bars_path, compression="snappy", index=False)
        audit.to_csv(audit_path, index=False)
        print(f"{frequency}m bars: {len(bars)}; saved: {bars_path}")
        print(f"{frequency}m audit: {audit_path}")


if __name__ == "__main__":
    main()

