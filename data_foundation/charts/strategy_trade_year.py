from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.collections import LineCollection, PolyCollection


def plot_strategy_trade_year_charts(
    bars: pd.DataFrame,
    trades: pd.DataFrame,
    output_dir: str | Path,
    *,
    strategy_name: str,
    filename_prefix: str = "strategy_trade_chart",
    datetime_col: str = "datetime",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    trend_macd_col: str = "trend_macd",
    trend_macd_signal_col: str = "trend_macd_signal",
    trend_macd_hist_col: str = "trend_macd_hist",
    obv_col: str = "obv",
    obv_sma_col: str = "obv_sma40",
    show_dc_pivots: bool = True,
    dc_pivot_kind_col: str = "dc_pivot_kind",
    dc_pivot_price_col: str = "dc_pivot_price",
    min_width: float = 36.0,
    width_per_bar: float = 0.01,
    max_width: float | None = 88.0,
) -> list[Path]:
    """Draw one yearly 15m trade chart per calendar year.

    The input bars are expected to already contain the strategy's indicator
    columns. The function filters ``trades`` by ``strategy_name`` before
    validating trade/bar alignment.
    """
    _validate_dimensions(
        min_width=min_width,
        width_per_bar=width_per_bar,
        max_width=max_width,
    )
    prepared_bars = _prepare_bars(
        bars,
        datetime_col=datetime_col,
        open_col=open_col,
        high_col=high_col,
        low_col=low_col,
        close_col=close_col,
        trend_macd_col=trend_macd_col,
        trend_macd_signal_col=trend_macd_signal_col,
        trend_macd_hist_col=trend_macd_hist_col,
        obv_col=obv_col,
        obv_sma_col=obv_sma_col,
        show_dc_pivots=show_dc_pivots,
        dc_pivot_kind_col=dc_pivot_kind_col,
        dc_pivot_price_col=dc_pivot_price_col,
    )
    prepared_trades = _prepare_trades(trades, strategy_name=strategy_name)
    _validate_trade_alignment(prepared_bars, prepared_trades)

    years = sorted(prepared_bars["datetime"].dt.year.unique())
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for year in years:
        year_bars = prepared_bars[prepared_bars["datetime"].dt.year == year].copy()
        year_bars = year_bars.reset_index(drop=True)
        year_bars["x"] = range(len(year_bars))
        time_to_x = dict(zip(year_bars["datetime"], year_bars["x"]))
        year_trade_points = _trade_points_for_year(prepared_trades, int(year), time_to_x)
        path = output / f"{filename_prefix}_{int(year)}.png"
        _draw_year_chart(
            year_bars,
            year_trade_points,
            path,
            strategy_name=strategy_name,
            year=int(year),
            show_dc_pivots=show_dc_pivots,
            min_width=min_width,
            width_per_bar=width_per_bar,
            max_width=max_width,
        )
        paths.append(path)
    return paths


def _prepare_bars(
    frame: pd.DataFrame,
    *,
    datetime_col: str,
    open_col: str,
    high_col: str,
    low_col: str,
    close_col: str,
    trend_macd_col: str,
    trend_macd_signal_col: str,
    trend_macd_hist_col: str,
    obv_col: str,
    obv_sma_col: str,
    show_dc_pivots: bool,
    dc_pivot_kind_col: str,
    dc_pivot_price_col: str,
) -> pd.DataFrame:
    required = [
        datetime_col,
        open_col,
        high_col,
        low_col,
        close_col,
        trend_macd_col,
        trend_macd_signal_col,
        trend_macd_hist_col,
        obv_col,
        obv_sma_col,
    ]
    if show_dc_pivots:
        required.extend([dc_pivot_kind_col, dc_pivot_price_col])
    _require_columns(frame, required, frame_name="bars")

    data = {
        "datetime": _naive_datetimes(frame[datetime_col]),
        "open": pd.to_numeric(frame[open_col], errors="coerce"),
        "high": pd.to_numeric(frame[high_col], errors="coerce"),
        "low": pd.to_numeric(frame[low_col], errors="coerce"),
        "close": pd.to_numeric(frame[close_col], errors="coerce"),
        "trend_macd": pd.to_numeric(frame[trend_macd_col], errors="coerce"),
        "trend_macd_signal": pd.to_numeric(
            frame[trend_macd_signal_col], errors="coerce"
        ),
        "trend_macd_hist": pd.to_numeric(frame[trend_macd_hist_col], errors="coerce"),
        "obv": pd.to_numeric(frame[obv_col], errors="coerce"),
        "obv_sma40": pd.to_numeric(frame[obv_sma_col], errors="coerce"),
    }
    if show_dc_pivots:
        data["dc_pivot_kind"] = frame[dc_pivot_kind_col].astype("object")
        data["dc_pivot_price"] = pd.to_numeric(frame[dc_pivot_price_col], errors="coerce")
    else:
        data["dc_pivot_kind"] = pd.Series([None] * len(frame), index=frame.index, dtype="object")
        data["dc_pivot_price"] = pd.Series([pd.NA] * len(frame), index=frame.index, dtype="object")

    bars = pd.DataFrame(data)
    bars = (
        bars.dropna(subset=["datetime", "open", "high", "low", "close"])
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    if bars.empty:
        raise ValueError("bars has no plottable rows")
    duplicated = bars.loc[bars["datetime"].duplicated(), "datetime"]
    if not duplicated.empty:
        first_duplicate = duplicated.iloc[0]
        raise ValueError(f"bars contains duplicate datetime: {first_duplicate}")
    return bars


def _prepare_trades(trades: pd.DataFrame, *, strategy_name: str) -> pd.DataFrame:
    required = [
        "strategy",
        "entry_time",
        "exit_time",
        "side",
        "entry_close",
        "exit_close",
        "net_return_points",
    ]
    _require_columns(trades, required, frame_name="trades")
    selected = trades[trades["strategy"] == strategy_name].copy().reset_index(drop=True)
    if selected.empty:
        return pd.DataFrame(columns=required + ["trade_number"])

    selected["trade_number"] = range(1, len(selected) + 1)
    selected["entry_time"] = _naive_datetimes(selected["entry_time"])
    selected["exit_time"] = _naive_datetimes(selected["exit_time"])
    selected["side"] = pd.to_numeric(selected["side"], errors="coerce").astype("Int64")
    selected["entry_close"] = pd.to_numeric(selected["entry_close"], errors="coerce")
    selected["exit_close"] = pd.to_numeric(selected["exit_close"], errors="coerce")
    selected["net_return_points"] = pd.to_numeric(
        selected["net_return_points"], errors="coerce"
    )
    if "exit_reason" not in selected.columns:
        selected["exit_reason"] = ""
    if selected[["entry_time", "exit_time", "side", "entry_close", "exit_close"]].isna().any().any():
        raise ValueError("trades contains invalid entry/exit time, side, or close value")
    return selected


def _validate_trade_alignment(bars: pd.DataFrame, trades: pd.DataFrame) -> None:
    if trades.empty:
        return
    bars_by_time = bars.set_index("datetime")[["high", "low", "close"]].to_dict("index")
    for _, trade in trades.iterrows():
        trade_number = int(trade["trade_number"])
        for point_name, time_col, price_col in [
            ("entry", "entry_time", "entry_close"),
            ("exit", "exit_time", "exit_close"),
        ]:
            timestamp = trade[time_col]
            if timestamp not in bars_by_time:
                raise ValueError(
                    f"trade #{trade_number} {point_name}_time {timestamp} "
                    "does not match any bar datetime"
                )
            bar = bars_by_time[timestamp]
            actual_close = float(trade[price_col])
            if point_name == "exit" and trade.get("exit_reason", "") == "structure_stop_exit":
                continue
            expected_close = float(bar["close"])
            if abs(expected_close - actual_close) > 1e-9:
                raise ValueError(
                    f"trade #{trade_number} {point_name}_close {actual_close} "
                    f"does not match bar close {expected_close} at {timestamp}"
                )


def _trade_points_for_year(
    trades: pd.DataFrame,
    year: int,
    time_to_x: dict[pd.Timestamp, int],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "trade_number",
                "point",
                "time",
                "x",
                "price",
                "side",
                "net_return_points",
                "same_year_trade",
            ]
        )

    for _, trade in trades.iterrows():
        entry_time = trade["entry_time"]
        exit_time = trade["exit_time"]
        same_year_trade = entry_time.year == year and exit_time.year == year
        if entry_time.year == year:
            rows.append(
                {
                    "trade_number": int(trade["trade_number"]),
                    "point": "entry",
                    "time": entry_time,
                    "x": time_to_x[entry_time],
                    "price": float(trade["entry_close"]),
                    "side": int(trade["side"]),
                    "net_return_points": float(trade["net_return_points"]),
                    "same_year_trade": same_year_trade,
                }
            )
        if exit_time.year == year:
            rows.append(
                {
                    "trade_number": int(trade["trade_number"]),
                    "point": "exit",
                    "time": exit_time,
                    "x": time_to_x[exit_time],
                    "price": float(trade["exit_close"]),
                    "side": int(trade["side"]),
                    "net_return_points": float(trade["net_return_points"]),
                    "same_year_trade": same_year_trade,
                }
            )
    return pd.DataFrame(rows)


def _draw_year_chart(
    bars: pd.DataFrame,
    trade_points: pd.DataFrame,
    output_path: Path,
    *,
    strategy_name: str,
    year: int,
    show_dc_pivots: bool,
    min_width: float,
    width_per_bar: float,
    max_width: float | None,
) -> None:
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    figure_width = max(min_width, len(bars) * width_per_bar)
    if max_width is not None:
        figure_width = min(figure_width, max_width)

    fig, (ax_price, ax_macd, ax_obv) = plt.subplots(
        3,
        1,
        figsize=(figure_width, 10.5),
        dpi=150,
        sharex=True,
        gridspec_kw={"height_ratios": [3.4, 1.3, 1.15], "hspace": 0.08},
    )

    _draw_candles(ax_price, bars)
    if show_dc_pivots:
        _draw_dc_pivot_markers(ax_price, bars)
    _draw_trade_markers(ax_price, trade_points)
    _expand_price_axis_for_trade_points(ax_price, trade_points)
    _draw_macd(ax_macd, bars)
    _draw_obv(ax_obv, bars)
    _set_month_ticks(ax_obv, bars)

    entry_count = int((trade_points["point"] == "entry").sum()) if not trade_points.empty else 0
    exit_count = int((trade_points["point"] == "exit").sum()) if not trade_points.empty else 0
    ax_price.set_title(
        f"{strategy_name} {year} 15m trades "
        f"(entries={entry_count}, exits={exit_count})"
    )
    ax_price.set_ylabel("Price")
    ax_price.grid(True, alpha=0.22)
    ax_macd.grid(True, alpha=0.22)
    ax_obv.grid(True, alpha=0.22)
    ax_price.legend(loc="best", ncol=6, fontsize=8)
    ax_macd.legend(loc="upper left", ncol=3, fontsize=8)
    ax_obv.legend(loc="upper left", ncol=2, fontsize=8)

    fig.subplots_adjust(left=0.055, right=0.985, top=0.94, bottom=0.08, hspace=0.08)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def _draw_candles(ax: Any, bars: pd.DataFrame) -> None:
    up_color = "#c00000"
    down_color = "#00a65a"
    candle_width = 0.62

    wick_segments: list[list[tuple[float, float]]] = []
    wick_colors: list[str] = []
    body_polygons: list[list[tuple[float, float]]] = []
    body_colors: list[str] = []
    doji_segments: list[list[tuple[float, float]]] = []
    doji_colors: list[str] = []

    for row in bars.itertuples(index=False):
        x_position = float(row.x)
        open_price = float(row.open)
        close_price = float(row.close)
        color = up_color if close_price >= open_price else down_color
        wick_segments.append([(x_position, float(row.low)), (x_position, float(row.high))])
        wick_colors.append(color)

        if close_price == open_price:
            doji_segments.append(
                [
                    (x_position - candle_width / 2, close_price),
                    (x_position + candle_width / 2, close_price),
                ]
            )
            doji_colors.append(color)
            continue

        bottom = min(open_price, close_price)
        top = max(open_price, close_price)
        body_polygons.append(
            [
                (x_position - candle_width / 2, bottom),
                (x_position - candle_width / 2, top),
                (x_position + candle_width / 2, top),
                (x_position + candle_width / 2, bottom),
            ]
        )
        body_colors.append(color)

    ax.add_collection(
        LineCollection(wick_segments, colors=wick_colors, linewidths=0.65, alpha=0.95)
    )
    if body_polygons:
        ax.add_collection(
            PolyCollection(
                body_polygons,
                facecolors=body_colors,
                edgecolors=body_colors,
                linewidths=0.35,
                alpha=0.88,
            )
        )
    if doji_segments:
        ax.add_collection(
            LineCollection(doji_segments, colors=doji_colors, linewidths=0.85, alpha=0.95)
        )

    ax.set_xlim(-1, len(bars))
    price_min = float(bars["low"].min())
    price_max = float(bars["high"].max())
    margin = max((price_max - price_min) * 0.03, 1.0)
    ax.set_ylim(price_min - margin, price_max + margin)


def _draw_trade_markers(ax: Any, trade_points: pd.DataFrame) -> None:
    if trade_points.empty:
        return

    entry_points = trade_points[trade_points["point"] == "entry"]
    long_entries = entry_points[entry_points["side"] > 0]
    short_entries = entry_points[entry_points["side"] < 0]
    exits = trade_points[trade_points["point"] == "exit"]

    if not long_entries.empty:
        ax.scatter(
            long_entries["x"],
            long_entries["price"],
            marker="^",
            s=64,
            color="#c00000",
            edgecolors="#202020",
            linewidths=0.55,
            zorder=5,
            label="long entry",
        )
    if not short_entries.empty:
        ax.scatter(
            short_entries["x"],
            short_entries["price"],
            marker="v",
            s=64,
            color="#00a65a",
            edgecolors="#202020",
            linewidths=0.55,
            zorder=5,
            label="short entry",
        )
    if not exits.empty:
        ax.scatter(
            exits["x"],
            exits["price"],
            marker="x",
            s=58,
            color="#111111",
            linewidths=1.2,
            zorder=6,
            label="exit",
        )

    complete_trade_numbers = sorted(
        set(entry_points.loc[entry_points["same_year_trade"], "trade_number"])
        & set(exits.loc[exits["same_year_trade"], "trade_number"])
    )
    for trade_number in complete_trade_numbers:
        entry = entry_points[entry_points["trade_number"] == trade_number].iloc[0]
        exit_ = exits[exits["trade_number"] == trade_number].iloc[0]
        profit = float(exit_["net_return_points"])
        line_color = "#c00000" if profit >= 0 else "#555555"
        ax.plot(
            [entry["x"], exit_["x"]],
            [entry["price"], exit_["price"]],
            color=line_color,
            linewidth=0.7,
            alpha=0.58,
            zorder=4,
        )
        ax.annotate(
            f"#{trade_number} {profit:+.0f}",
            xy=(exit_["x"], exit_["price"]),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=6.5,
            color="#202020",
            ha="left",
            va="bottom",
        )


def _expand_price_axis_for_trade_points(ax: Any, trade_points: pd.DataFrame) -> None:
    if trade_points.empty:
        return
    axis_low, axis_high = ax.get_ylim()
    point_low = float(trade_points["price"].min())
    point_high = float(trade_points["price"].max())
    new_low = min(float(axis_low), point_low)
    new_high = max(float(axis_high), point_high)
    if new_low == axis_low and new_high == axis_high:
        return
    margin = max((new_high - new_low) * 0.03, 1.0)
    ax.set_ylim(new_low - margin, new_high + margin)


def _draw_dc_pivot_markers(ax: Any, bars: pd.DataFrame) -> None:
    peaks = bars[bars["dc_pivot_kind"] == "peak"]
    valleys = bars[bars["dc_pivot_kind"] == "valley"]

    if not peaks.empty:
        ax.scatter(
            peaks["x"],
            peaks["dc_pivot_price"],
            marker="v",
            s=42,
            facecolors="none",
            edgecolors="#2b2b2b",
            linewidths=0.85,
            alpha=0.8,
            zorder=4,
            label="DC peak",
        )
    if not valleys.empty:
        ax.scatter(
            valleys["x"],
            valleys["dc_pivot_price"],
            marker="^",
            s=42,
            facecolors="none",
            edgecolors="#2b2b2b",
            linewidths=0.85,
            alpha=0.8,
            zorder=4,
            label="DC valley",
        )


def _draw_macd(ax: Any, bars: pd.DataFrame) -> None:
    hist_colors = ["#c00000" if value >= 0 else "#00a65a" for value in bars["trend_macd_hist"]]
    ax.bar(
        bars["x"],
        bars["trend_macd_hist"],
        color=hist_colors,
        alpha=0.42,
        width=0.78,
        label="trend MACD hist",
    )
    ax.plot(bars["x"], bars["trend_macd"], color="#1f77b4", linewidth=1.05, label="DIF")
    ax.plot(
        bars["x"],
        bars["trend_macd_signal"],
        color="#ff7f0e",
        linewidth=1.05,
        label="DEA",
    )
    ax.axhline(0, color="#606060", linewidth=0.8, alpha=0.7)
    ax.set_ylabel("MACD")


def _draw_obv(ax: Any, bars: pd.DataFrame) -> None:
    ax.plot(bars["x"], bars["obv"], color="#7030a0", linewidth=1.05, label="OBV")
    ax.plot(
        bars["x"],
        bars["obv_sma40"],
        color="#00a6d6",
        linewidth=1.05,
        linestyle="--",
        label="OBV SMA40",
    )
    ax.axhline(0, color="#606060", linewidth=0.8, alpha=0.6)
    ax.set_ylabel("OBV")
    ax.set_xlabel("15m bars in compressed trading sequence")
    ax.ticklabel_format(style="plain", axis="y", useOffset=False)


def _set_month_ticks(ax: Any, bars: pd.DataFrame) -> None:
    month_keys = bars["datetime"].dt.to_period("M")
    raw_ticks = bars.groupby(month_keys, sort=True)["x"].first().to_list()
    raw_labels = [
        timestamp.strftime("%Y-%m")
        for timestamp in bars.groupby(month_keys, sort=True)["datetime"].first()
    ]
    minimum_tick_gap = max(len(bars) * 0.035, 80)
    ticks: list[int] = []
    labels: list[str] = []
    for tick, label in zip(raw_ticks, raw_labels):
        if not ticks or tick - ticks[-1] >= minimum_tick_gap:
            ticks.append(int(tick))
            labels.append(label)

    last_x = int(bars["x"].iloc[-1])
    if ticks and ticks[-1] != last_x and last_x - ticks[-1] >= minimum_tick_gap:
        ticks.append(int(bars["x"].iloc[-1]))
        labels.append(bars["datetime"].iloc[-1].strftime("%Y-%m-%d"))
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)


def _naive_datetimes(values: Any) -> pd.Series:
    timestamps = pd.to_datetime(values)
    if getattr(timestamps.dt, "tz", None) is not None:
        timestamps = timestamps.dt.tz_localize(None)
    return timestamps


def _validate_dimensions(
    *,
    min_width: float,
    width_per_bar: float,
    max_width: float | None,
) -> None:
    if min_width <= 0:
        raise ValueError("min_width must be positive")
    if width_per_bar <= 0:
        raise ValueError("width_per_bar must be positive")
    if max_width is not None and max_width <= 0:
        raise ValueError("max_width must be positive")
    if max_width is not None and max_width < min_width:
        raise ValueError("max_width must be greater than or equal to min_width")


def _require_columns(frame: pd.DataFrame, columns: list[str], *, frame_name: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{frame_name} missing required column(s): {', '.join(missing)}")
