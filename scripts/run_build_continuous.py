from __future__ import annotations

import argparse
from pathlib import Path

import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data_foundation import build_continuous_contract, load_cleaned_1m


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Build RB continuous main contract.")
    parser.add_argument("--input", type=Path, default=Path("data/processed/RB_1m_raw_clean.parquet"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/RB_1m_continuous.parquet"))
    parser.add_argument("--switch-log", type=Path, default=Path("data/processed/RB_main_switch_log.csv"))
    parser.add_argument(
        "--method",
        choices=["backward_adjusted", "no_adjust"],
        default="backward_adjusted",
    )
    return parser.parse_args()


def main() -> None:
    """Load cleaned bars, build the continuous series, and persist outputs."""
    args = parse_arguments()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.switch_log.parent.mkdir(parents=True, exist_ok=True)

    cleaned = load_cleaned_1m(args.input)
    continuous, switch_log = build_continuous_contract(cleaned, method=args.method)
    continuous.to_parquet(args.output, compression="snappy", index=False)
    switch_log.to_csv(args.switch_log, index=False)

    cumulative = 0.0
    if not switch_log.empty:
        cumulative = float(switch_log["cumulative_adjustment"].iloc[-1])
    print(f"continuous rows: {len(continuous)}")
    print(f"switches: {len(switch_log)}")
    print(f"cumulative adjustment: {cumulative}")
    print(f"saved: {args.output}")
    print(f"switch log: {args.switch_log}")


if __name__ == "__main__":
    main()
