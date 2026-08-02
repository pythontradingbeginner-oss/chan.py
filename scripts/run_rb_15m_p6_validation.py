"""Run P6 RB 15m rolling OOS validation and historical paper replay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chan_futures.config_loader import load_config
from chan_futures.production import (
    PaperReplayRunner,
    ProductionWalkforwardRunner,
    load_rb_production_frames,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/rb_15m_qingpai_strict.yaml")
    parser.add_argument("--output", default="reports/rb_15m_p6")
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=3)
    parser.add_argument("--step-months", type=int, default=3)
    parser.add_argument("--max-folds", type=int)
    parser.add_argument("--paper-bars", type=int, default=500)
    args = parser.parse_args()

    config = load_config(PROJECT_ROOT / args.config)
    frames = load_rb_production_frames(config)
    output = PROJECT_ROOT / args.output
    summary = ProductionWalkforwardRunner(
        config,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        max_folds=args.max_folds,
    ).run(frames=frames, output_dir=output / "walkforward")
    paper = PaperReplayRunner(config, paper_bars=args.paper_bars).run(
        frames=frames,
        output_dir=output / "paper",
    )
    print(summary.to_string(index=False))
    print(
        "paper:",
        len(paper.bars),
        "bars,",
        len(paper.trades),
        "trades,",
        len(paper.decision_trace),
        "decisions",
    )


if __name__ == "__main__":
    main()
