from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt
import pandas as pd

from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_dc_structure_candidate_diagnostics import _candidate_features
from run_final_15m_strategy_obv_filter_compare import _append_obv
from run_final_15m_strategy_vnpy_backtest import FINAL_CANDIDATE, START, END, _restrict_range


TARGET_STRATEGY = "final_15m_obv_sma40_structure_stop"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot a single structure-stop trade with DC pivots and stop line."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument(
        "--trades-path",
        type=Path,
        default=Path("reports/final_15m_structure_stop_compare_trades.csv"),
    )
    parser.add_argument(
        "--entry-time",
        default="2021-09-24 09:45:00",
    )
    parser.add_argument(
        "--strategy-name",
        default=TARGET_STRATEGY,
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=Path("reports/single_trade_20210924_0945_structure_stop.png"),
    )
    parser.add_argument(
        "--pad-bars",
        type=int,
        default=24,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    trade = _load_trade(
        args.trades_path,
        strategy_name=args.strategy_name,
        entry_time=pd.Timestamp(args.entry_time),
    )
    featured = _load_featured_bars(args.continuous_path)
    window = _trade_window(featured, trade, pad_bars=args.pad_bars)
    _plot_trade_window(window, trade, args.output_path)
    print(args.output_path)


def _load_trade(trades_path: Path, *, strategy_name: str, entry_time: pd.Timestamp) -> pd.Series:
    trades = pd.read_csv(
        trades_path,
        encoding="utf-8-sig",
        parse_dates=["entry_time", "exit_time", "entry_structure_stop_timestamp"],
    )
    matched = trades[
        (trades["strategy"] == strategy_name)
        & (trades["entry_time"] == entry_time)
    ]
    if matched.empty:
        raise ValueError(f"trade not found for {strategy_name} at {entry_time}")
    return matched.iloc[0]


def _load_featured_bars(continuous_path: Path) -> pd.DataFrame:
    start = pd.Timestamp(START)
    end = pd.Timestamp(END)
    continuous = load_continuous_1m(continuous_path)
    continuous = _restrict_range(continuous, start, end)
    bars_15m = aggregate_continuous_1m_to_Nm(continuous, FINAL_CANDIDATE.freq_minutes)
    bars_15m = _restrict_range(bars_15m, start, end)
    return _append_obv(_candidate_features(bars_15m, FINAL_CANDIDATE), window=40)


def _trade_window(featured: pd.DataFrame, trade: pd.Series, *, pad_bars: int) -> pd.DataFrame:
    entry_time = pd.Timestamp(trade["entry_time"])
    exit_time = pd.Timestamp(trade["exit_time"])
    entry_index = int(featured.index[featured["datetime"] == entry_time][0])
    exit_index = int(featured.index[featured["datetime"] == exit_time][0])
    start_index = max(0, entry_index - pad_bars)
    end_index = min(len(featured) - 1, exit_index + pad_bars)
    window = featured.iloc[start_index : end_index + 1].copy().reset_index(drop=True)
    window["x"] = range(len(window))
    return window


def _plot_trade_window(window: pd.DataFrame, trade: pd.Series, output_path: Path) -> None:
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure_width = max(20.0, min(len(window) * 0.24, 40.0))
    fig, (ax_price, ax_macd, ax_obv) = plt.subplots(
        3,
        1,
        figsize=(figure_width, 10.5),
        dpi=180,
        sharex=True,
        gridspec_kw={"height_ratios": [3.3, 1.25, 1.15], "hspace": 0.08},
    )

    x = window["x"]
    ax_price.plot(x, window["close"], color="#2f5597", linewidth=1.4, label="15m close")
    ax_price.fill_between(x, window["low"], window["high"], color="#8faadc", alpha=0.16)

    peaks = window[window["dc_pivot_kind"] == "peak"]
    valleys = window[window["dc_pivot_kind"] == "valley"]
    if not peaks.empty:
        ax_price.scatter(
            peaks["x"],
            peaks["dc_pivot_price"],
            marker="v",
            s=64,
            facecolors="none",
            edgecolors="#2b2b2b",
            linewidths=0.9,
            label="DC peak",
        )
    if not valleys.empty:
        ax_price.scatter(
            valleys["x"],
            valleys["dc_pivot_price"],
            marker="^",
            s=64,
            facecolors="none",
            edgecolors="#2b2b2b",
            linewidths=0.9,
            label="DC valley",
        )

    entry_time = pd.Timestamp(trade["entry_time"])
    exit_time = pd.Timestamp(trade["exit_time"])
    entry_x = int(window.index[window["datetime"] == entry_time][0])
    exit_x = int(window.index[window["datetime"] == exit_time][0])
    entry_price = float(trade["entry_close"])
    exit_price = float(trade["exit_close"])
    stop_price = float(trade["entry_structure_stop_price"])

    ax_price.scatter(
        [entry_x],
        [entry_price],
        marker="v",
        s=90,
        color="#00a65a",
        edgecolors="#202020",
        linewidths=0.65,
        zorder=6,
        label="short entry",
    )
    ax_price.scatter(
        [exit_x],
        [exit_price],
        marker="x",
        s=88,
        color="#111111",
        linewidths=1.5,
        zorder=7,
        label="structure stop exit",
    )
    ax_price.axhline(
        stop_price,
        color="#b03030",
        linestyle="--",
        linewidth=1.1,
        alpha=0.9,
        label=f"stop {stop_price:.0f}",
    )
    ax_price.axvline(entry_x, color="#00a65a", linestyle=":", linewidth=1.0, alpha=0.75)
    ax_price.axvline(exit_x, color="#111111", linestyle=":", linewidth=1.0, alpha=0.75)
    ax_price.annotate(
        f"entry {entry_price:.0f}\n{entry_time:%m-%d %H:%M}",
        xy=(entry_x, entry_price),
        xytext=(-14, -32),
        textcoords="offset points",
        fontsize=8,
        ha="right",
        va="top",
    )
    ax_price.annotate(
        f"stop exit {exit_price:.0f}\nPnL {float(trade['net_return_points']):+.0f}",
        xy=(exit_x, exit_price),
        xytext=(10, 10),
        textcoords="offset points",
        fontsize=8,
        ha="left",
        va="bottom",
    )

    hist_colors = ["#c00000" if value >= 0 else "#00a65a" for value in window["trend_macd_hist"]]
    ax_macd.bar(x, window["trend_macd_hist"], color=hist_colors, alpha=0.42, width=0.78, label="MACD hist")
    ax_macd.plot(x, window["trend_macd"], color="#1f77b4", linewidth=1.0, label="DIF")
    ax_macd.plot(x, window["trend_macd_signal"], color="#ff7f0e", linewidth=1.0, label="DEA")
    ax_macd.axhline(0, color="#606060", linewidth=0.8, alpha=0.7)

    ax_obv.plot(x, window["obv"], color="#7030a0", linewidth=1.0, label="OBV")
    ax_obv.plot(x, window["obv_sma40"], color="#00a6d6", linewidth=1.0, linestyle="--", label="OBV SMA40")
    ax_obv.axhline(0, color="#606060", linewidth=0.8, alpha=0.6)

    tick_positions = list(window.index[::max(len(window) // 10, 1)])
    if tick_positions[-1] != len(window) - 1:
        tick_positions.append(len(window) - 1)
    tick_labels = [window.loc[idx, "datetime"].strftime("%m-%d\n%H:%M") for idx in tick_positions]
    ax_obv.set_xticks(tick_positions)
    ax_obv.set_xticklabels(tick_labels, rotation=0, fontsize=8)

    ax_price.set_title(
        "2021-09-24 09:45 short trade stopped at 2021-09-28 21:15"
    )
    ax_price.set_ylabel("Price")
    ax_macd.set_ylabel("MACD")
    ax_obv.set_ylabel("OBV")
    ax_obv.set_xlabel("15m bars")
    for ax in (ax_price, ax_macd, ax_obv):
        ax.grid(True, alpha=0.22)
    ax_price.legend(loc="best", ncol=4, fontsize=8)
    ax_macd.legend(loc="upper left", ncol=3, fontsize=8)
    ax_obv.legend(loc="upper left", ncol=2, fontsize=8)

    fig.subplots_adjust(left=0.055, right=0.985, top=0.94, bottom=0.09, hspace=0.08)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
