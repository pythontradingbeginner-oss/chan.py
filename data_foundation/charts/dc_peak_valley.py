from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from data_foundation.features import add_dc_macd_comparison


def load_replay_bars_jsonl(path: str | Path) -> pd.DataFrame:
    """Load SteelPulse replay 5m bar JSONL into standard OHLCV columns."""
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows.append(
                {
                    "datetime": pd.Timestamp(
                        row.get("observer_available_at")
                        or row.get("bar_end_timestamp")
                        or row["bar_start_timestamp"]
                    ),
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row.get("volume", 0.0) or 0.0,
                    "contract_id": row.get("contract_id"),
                    "trading_day": row.get("trading_day"),
                }
            )
    return pd.DataFrame(rows)


def plot_dc_peak_valley_chart(
    frame: pd.DataFrame,
    output_path: str | Path,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    kline_minutes: int = 5,
    source_minutes: int = 5,
    dc_threshold_points: float = 20,
    trend_buffer_points: float = 15,
    obv_ma_period: int = 30,
    datetime_col: str = "datetime",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    volume_col: str = "volume",
    compress_gaps: bool = True,
    min_width: float = 14,
    width_per_bar: float = 0.08,
    max_width: float | None = None,
) -> Path:
    """Draw price, DC peaks/valleys, MACD, OBV, and OBV moving average."""
    _validate_plot_params(
        kline_minutes,
        source_minutes,
        obv_ma_period,
        min_width=min_width,
        width_per_bar=width_per_bar,
        max_width=max_width,
    )
    required = [datetime_col, open_col, high_col, low_col, close_col, volume_col]
    _require_columns(frame, required)

    bars = _prepare_bars(
        frame,
        datetime_col=datetime_col,
        open_col=open_col,
        high_col=high_col,
        low_col=low_col,
        close_col=close_col,
        volume_col=volume_col,
    )
    bars = _filter_time_window(bars, start=start, end=end)
    bars = _aggregate_by_sequence(
        bars,
        kline_minutes=kline_minutes,
        source_minutes=source_minutes,
    )
    if bars.empty:
        raise ValueError("no bars available for the requested time window and kline_minutes")

    bars = bars.reset_index(drop=True)
    bars = add_dc_macd_comparison(
        bars,
        threshold_points=dc_threshold_points,
        trend_buffer_points=trend_buffer_points,
    )
    bars["obv"] = _calculate_obv(bars["close"], bars["volume"])
    bars["obv_ma"] = bars["obv"].rolling(window=obv_ma_period, min_periods=1).mean()
    bars["x"] = range(len(bars)) if compress_gaps else bars["datetime"]

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _draw_chart(
        bars,
        output,
        kline_minutes=kline_minutes,
        obv_ma_period=obv_ma_period,
        compress_gaps=compress_gaps,
        min_width=min_width,
        width_per_bar=width_per_bar,
        max_width=max_width,
    )
    return output


def _prepare_bars(
    frame: pd.DataFrame,
    *,
    datetime_col: str,
    open_col: str,
    high_col: str,
    low_col: str,
    close_col: str,
    volume_col: str,
) -> pd.DataFrame:
    bars = pd.DataFrame(
        {
            "datetime": pd.to_datetime(frame[datetime_col]),
            "open": pd.to_numeric(frame[open_col], errors="coerce"),
            "high": pd.to_numeric(frame[high_col], errors="coerce"),
            "low": pd.to_numeric(frame[low_col], errors="coerce"),
            "close": pd.to_numeric(frame[close_col], errors="coerce"),
            "volume": pd.to_numeric(frame[volume_col], errors="coerce").fillna(0.0),
        }
    )
    if "trading_day" in frame.columns:
        bars["trading_day"] = frame["trading_day"].to_numpy()

    return (
        bars.dropna(subset=["datetime", "open", "high", "low", "close"])
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def _filter_time_window(
    bars: pd.DataFrame,
    *,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
) -> pd.DataFrame:
    if bars.empty:
        return bars

    timestamps = bars["datetime"]
    if start is not None:
        start_ts = _coerce_bound(start, timestamps)
        bars = bars[timestamps >= start_ts]
        timestamps = bars["datetime"]
    if end is not None:
        end_ts = _coerce_bound(end, timestamps)
        bars = bars[timestamps <= end_ts]
    return bars.reset_index(drop=True)


def _aggregate_by_sequence(
    bars: pd.DataFrame,
    *,
    kline_minutes: int,
    source_minutes: int,
) -> pd.DataFrame:
    if kline_minutes == source_minutes:
        return bars.copy()

    bars_per_window = kline_minutes // source_minutes
    source = bars.copy().reset_index(drop=True)
    gaps = source["datetime"].diff() > pd.Timedelta(minutes=source_minutes * 1.5)
    if "trading_day" in source.columns:
        gaps = gaps | (source["trading_day"] != source["trading_day"].shift(1))
    source["segment"] = gaps.fillna(False).cumsum()
    source["bar_number"] = source.groupby("segment").cumcount()
    source["window_id"] = source["bar_number"] // bars_per_window

    grouped = (
        source.groupby(["segment", "window_id"], sort=True)
        .agg(
            source_count=("datetime", "size"),
            datetime=("datetime", "last"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index(drop=True)
    )
    complete = grouped[grouped["source_count"] == bars_per_window].copy()
    return complete.drop(columns=["source_count"]).reset_index(drop=True)


def _draw_chart(
    bars: pd.DataFrame,
    output_path: Path,
    *,
    kline_minutes: int,
    obv_ma_period: int,
    compress_gaps: bool,
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
        figsize=(figure_width, 10),
        dpi=150,
        sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.25, 1.15], "hspace": 0.08},
    )

    x = bars["x"]
    peak_color = "#c00000"
    valley_color = "#00a65a"

    ax_price.plot(x, bars["close"], color="#2f5597", linewidth=1.5, label=f"{kline_minutes}m close")
    ax_price.fill_between(x, bars["low"], bars["high"], color="#8faadc", alpha=0.16, label=f"{kline_minutes}m high-low")
    _draw_dc_points(ax_price, bars, peak_color=peak_color, valley_color=valley_color)
    if compress_gaps:
        _draw_gap_markers([ax_price, ax_macd, ax_obv], bars)

    hist_colors = ["#2ca02c" if value >= 0 else "#d62728" for value in bars["close_macd_hist"]]
    ax_macd.bar(x, bars["close_macd_hist"], color=hist_colors, alpha=0.42, width=0.78, label="MACD hist")
    ax_macd.plot(x, bars["close_macd"], color="#1f77b4", linewidth=1.15, label="DIF")
    ax_macd.plot(x, bars["close_macd_signal"], color="#ff7f0e", linewidth=1.15, label="DEA")
    ax_macd.axhline(0, color="#606060", linewidth=0.8, alpha=0.7)
    ax_macd.set_ylabel("MACD")
    ax_macd.grid(True, alpha=0.22)
    ax_macd.legend(loc="upper left", ncol=3, fontsize=8)

    ax_obv.plot(x, bars["obv"], color="#7030a0", linewidth=1.25, label="OBV")
    ax_obv.plot(
        x,
        bars["obv_ma"],
        color="#00a6d6",
        linewidth=1.25,
        linestyle="--",
        label=f"OBV MA{obv_ma_period}",
    )
    ax_obv.axhline(0, color="#606060", linewidth=0.8, alpha=0.6)
    ax_obv.set_ylabel("OBV")
    ax_obv.grid(True, alpha=0.22)
    ax_obv.legend(loc="upper left", ncol=2, fontsize=8)

    if compress_gaps:
        tick_positions, tick_labels = _compressed_ticks(bars)
        ax_obv.set_xticks(tick_positions)
        ax_obv.set_xticklabels(tick_labels)
        ax_obv.set_xlabel(f"{kline_minutes}m bars in sequence")
    else:
        fig.autofmt_xdate(rotation=0)
        ax_obv.set_xlabel("Time")

    ax_price.set_title(f"DC Peaks and Valleys with MACD and OBV MA{obv_ma_period}")
    ax_price.set_ylabel("Price")
    ax_price.grid(True, alpha=0.25)
    ax_price.legend(loc="best", ncol=2, fontsize=8)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.94, bottom=0.08, hspace=0.08)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def _draw_dc_points(
    ax: Any,
    bars: pd.DataFrame,
    *,
    peak_color: str,
    valley_color: str,
) -> None:
    peaks = bars[bars["dc_pivot_kind"] == "peak"]
    valleys = bars[bars["dc_pivot_kind"] == "valley"]

    ax.scatter(peaks["x"], peaks["dc_pivot_price"], marker="v", s=90, color=peak_color, label="DC peak")
    ax.scatter(valleys["x"], valleys["dc_pivot_price"], marker="^", s=90, color=valley_color, label="DC valley")
    ax.scatter(peaks["x"], peaks["dc_confirmation_price"], marker="x", s=60, color=peak_color, linewidths=1.8, label="peak confirmation")
    ax.scatter(valleys["x"], valleys["dc_confirmation_price"], marker="x", s=60, color=valley_color, linewidths=1.8, label="valley confirmation")
    ax.plot(peaks["x"], peaks["dc_pivot_price"], color=peak_color, linestyle="--", linewidth=1.0, alpha=0.65, label="peak line")
    ax.plot(valleys["x"], valleys["dc_pivot_price"], color=valley_color, linestyle="--", linewidth=1.0, alpha=0.65, label="valley line")


def _draw_gap_markers(axes: list[Any], bars: pd.DataFrame) -> None:
    gaps = bars["datetime"].diff() > pd.Timedelta(minutes=_median_bar_minutes(bars) * 1.5)
    for gap_index in bars.index[gaps.fillna(False)]:
        x_position = gap_index - 0.5
        for ax in axes:
            ax.axvline(x_position, color="#808080", linestyle=":", linewidth=1.0, alpha=0.75)


def _compressed_ticks(bars: pd.DataFrame) -> tuple[list[int], list[str]]:
    if bars.empty:
        return [], []
    tick_positions: list[int] = []
    tick_labels: list[str] = []
    for row_number, timestamp in enumerate(bars["datetime"]):
        if (
            row_number == 0
            or row_number == len(bars) - 1
            or timestamp.minute == 0
            or bars["datetime"].diff().iloc[row_number] > pd.Timedelta(minutes=_median_bar_minutes(bars) * 1.5)
        ):
            tick_positions.append(row_number)
            tick_labels.append(timestamp.strftime("%m-%d\n%H:%M"))
    return tick_positions, tick_labels


def _calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    values = [0.0]
    for i in range(1, len(close)):
        if close.iloc[i] > close.iloc[i - 1]:
            values.append(values[-1] + volume.iloc[i])
        elif close.iloc[i] < close.iloc[i - 1]:
            values.append(values[-1] - volume.iloc[i])
        else:
            values.append(values[-1])
    return pd.Series(values, index=close.index, dtype="float64")


def _median_bar_minutes(bars: pd.DataFrame) -> float:
    deltas = bars["datetime"].diff().dropna()
    if deltas.empty:
        return 5.0
    return max(deltas.dt.total_seconds().div(60).median(), 1.0)


def _coerce_bound(value: str | pd.Timestamp, timestamps: pd.Series) -> pd.Timestamp:
    bound = pd.Timestamp(value)
    timezone = getattr(timestamps.dt, "tz", None)
    if timezone is not None and bound.tzinfo is None:
        return bound.tz_localize(timezone)
    if timezone is None and bound.tzinfo is not None:
        return bound.tz_localize(None)
    return bound


def _validate_plot_params(
    kline_minutes: int,
    source_minutes: int,
    obv_ma_period: int,
    *,
    min_width: float,
    width_per_bar: float,
    max_width: float | None,
) -> None:
    if source_minutes <= 0:
        raise ValueError("source_minutes must be positive")
    if kline_minutes <= 0:
        raise ValueError("kline_minutes must be positive")
    if kline_minutes < source_minutes:
        raise ValueError("kline_minutes must be greater than or equal to source_minutes")
    if kline_minutes % source_minutes != 0:
        raise ValueError("kline_minutes must be a multiple of source_minutes")
    if obv_ma_period <= 0:
        raise ValueError("obv_ma_period must be positive")
    if min_width <= 0:
        raise ValueError("min_width must be positive")
    if width_per_bar <= 0:
        raise ValueError("width_per_bar must be positive")
    if max_width is not None and max_width <= 0:
        raise ValueError("max_width must be positive")
    if max_width is not None and max_width < min_width:
        raise ValueError("max_width must be greater than or equal to min_width")


def _require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")
