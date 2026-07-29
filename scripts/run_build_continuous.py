from __future__ import annotations

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_foundation import build_continuous_products, load_cleaned_1m


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build raw and adjusted RB continuous products.")
    parser.add_argument("--input", type=Path, default=Path("data/processed/RB_1m_raw_clean.parquet"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--volume-threshold", type=int, default=100_000)
    parser.add_argument("--cooldown-days", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cleaned = load_cleaned_1m(args.input)
    raw, adjusted, switch_log, decisions = build_continuous_products(
        cleaned,
        volume_threshold=args.volume_threshold,
        cooldown_days=args.cooldown_days,
    )
    raw_path = args.output_dir / "RB_1m_continuous_raw.parquet"
    adjusted_path = args.output_dir / "RB_1m_continuous_adjusted.parquet"
    switch_path = args.output_dir / "RB_main_switch_log.csv"
    decision_path = args.output_dir / "RB_main_selection_audit.csv"
    raw.to_parquet(raw_path, compression="snappy", index=False)
    adjusted.to_parquet(adjusted_path, compression="snappy", index=False)
    switch_log.to_csv(switch_path, index=False)
    decisions.to_csv(decision_path, index=False)
    print(f"continuous rows: {len(raw)}")
    print(f"switches: {len(switch_log)}")
    print(f"saved raw: {raw_path}")
    print(f"saved adjusted: {adjusted_path}")
    print(f"switch log: {switch_path}")
    print(f"selection audit: {decision_path}")


if __name__ == "__main__":
    main()

