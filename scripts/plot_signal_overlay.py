"""将 signal_verify 产出的信号叠加到价格图上, 做可视化比对。

用法:
  python scripts/plot_signal_overlay.py \
    --events-csv reports/signal_verify_04/signal_events.csv \
    --assess-csv reports/signal_verify_04/signal_assessments.csv \
    --bars-csv reports/signal_verify_04/signal_bars.csv \
    --out reports/signal_verify_04/signal_overlay.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pandas as pd
import numpy as np


GRADE_COLORS = {
    "ideal": "#2ecc71",
    "standard": "#3498db",
    "weak": "#e74c3c",
}
BSP_MARKERS = {
    "1": "v",
    "1p": "^",
    "2": "s",
    "2s": "D",
    "3a": "P",
    "3b": "X",
}
# 买/卖点颜色偏移
BUY_SCATTER_EDGE = "#27ae60"
SELL_SCATTER_EDGE = "#c0392b"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Overlay extracted signals on price chart.")
    p.add_argument("--events-csv", type=Path, required=True)
    p.add_argument("--assess-csv", type=Path, required=True)
    p.add_argument("--bars-csv", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--grade-filter", type=str, default="ideal,standard,weak",
                   help="comma-sep grades to plot")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    events = pd.read_csv(args.events_csv, parse_dates=["bar_end_time", "available_at"])
    assess = pd.read_csv(args.assess_csv)
    bars = pd.read_csv(args.bars_csv, parse_dates=["datetime"])

    accepted_grades = {g.strip() for g in args.grade_filter.split(",")}
    assess = assess[assess["grade"].isin(accepted_grades)]

    # merge events + assessments on event_id
    merged = events.merge(
        assess[["event_id", "grade", "structural_score", "hard_blockers"]],
        on="event_id",
        how="inner",
    )

    # keep only latest revision per signal_key for visual clarity
    merged["_rn"] = merged.groupby("signal_key")["revision"].rank("dense", ascending=False)
    latest = merged[merged["_rn"] == 1].copy()

    if latest.empty:
        print("No signals to plot.")
        return

    fig, ax = plt.subplots(figsize=(28, 10))

    # ── price line ──
    ax.plot(bars["datetime"], bars["close"], color="black", linewidth=0.5, alpha=0.7, zorder=1)
    ax.set_ylabel("Close Price")
    ax.set_title(
        f"Chan.py Signal Overlay — {len(latest)} signals "
        f"({bars['datetime'].iloc[0].date()} → {bars['datetime'].iloc[-1].date()})"
    )

    # ── scatter signals ──
    for grade in ("ideal", "standard", "weak"):
        subset = latest[latest["grade"] == grade]
        if subset.empty:
            continue
        for bsp_type, marker in BSP_MARKERS.items():
            bsp_subset = subset[subset["primary_bsp"] == bsp_type]
            if bsp_subset.empty:
                continue
            # separate buy vs sell
            buy = bsp_subset[bsp_subset["direction"] == "long"]
            sell = bsp_subset[bsp_subset["direction"] == "short"]
            if not buy.empty:
                ax.scatter(
                    buy["bar_end_time"], buy["reference_price"],
                    marker=marker, s=70, zorder=5,
                    facecolors=GRADE_COLORS.get(grade, "gray"),
                    edgecolors=BUY_SCATTER_EDGE, linewidths=1.2,
                    label=f"{grade}/{bsp_type}/buy" if len(buy) <= 3 else "",
                )
            if not sell.empty:
                ax.scatter(
                    sell["bar_end_time"], sell["reference_price"],
                    marker=marker, s=70, zorder=5,
                    facecolors=GRADE_COLORS.get(grade, "gray"),
                    edgecolors=SELL_SCATTER_EDGE, linewidths=1.2,
                    label=f"{grade}/{bsp_type}/sell" if len(sell) <= 3 else "",
                )

    # ── legend ──
    legend_elements = [
        mpatches.Patch(color="black", alpha=0.6, label="Close Price"),
    ]
    for grade, color in GRADE_COLORS.items():
        if grade in accepted_grades:
            legend_elements.append(mpatches.Patch(color=color, alpha=0.7, label=f"Grade: {grade}"))
    ax.legend(handles=legend_elements, loc="upper left", fontsize=8)

    # ── marker annotation legend ──
    marker_texts = []
    for bsp, marker in BSP_MARKERS.items():
        cnt = len(latest[latest["primary_bsp"] == bsp])
        marker_texts.append(f"{marker}: {bsp} (n={cnt})")
    ax.text(
        0.99, 0.98, "\n".join(marker_texts),
        transform=ax.transAxes, fontsize=7, verticalalignment="top",
        horizontalalignment="right",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8),
    )

    fig.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(args.out), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"saved → {args.out}")


if __name__ == "__main__":
    main()
