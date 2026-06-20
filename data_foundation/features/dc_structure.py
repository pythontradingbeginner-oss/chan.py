from __future__ import annotations

from collections import deque
from typing import Any, Literal

import pandas as pd

from data_foundation.pivots import DCEvent, add_dc_pivots


class _TrendClassifier:
    def __init__(self, buffer_points: float) -> None:
        if buffer_points < 0:
            raise ValueError("trend_buffer_points must not be negative")

        self.buffer_points = buffer_points
        self.peaks: deque[DCEvent] = deque(maxlen=2)
        self.valleys: deque[DCEvent] = deque(maxlen=2)
        self.current_trend = "unknown"

    def update(self, event: DCEvent) -> str:
        if event.kind == "peak":
            self.peaks.append(event)
        elif event.kind == "valley":
            self.valleys.append(event)
        else:
            raise ValueError(f"unsupported pivot kind: {event.kind}")

        if len(self.peaks) < 2 or len(self.valleys) < 2:
            return self.current_trend

        self.current_trend = self._classify()
        return self.current_trend

    def _classify(self) -> str:
        previous_peak, latest_peak = self.peaks
        previous_valley, latest_valley = self.valleys

        peaks_rising = latest_peak.price >= previous_peak.price + self.buffer_points
        valleys_rising = latest_valley.price >= previous_valley.price + self.buffer_points
        if peaks_rising and valleys_rising:
            return "bull"

        peaks_falling = latest_peak.price <= previous_peak.price - self.buffer_points
        valleys_falling = latest_valley.price <= previous_valley.price - self.buffer_points
        if peaks_falling and valleys_falling:
            return "bear"

        return "sideways"


class _StickyTrendClassifier:
    def __init__(self, entry_buffer_points: float, exit_buffer_points: float) -> None:
        if entry_buffer_points < 0:
            raise ValueError("trend_entry_buffer_points must not be negative")
        if exit_buffer_points < 0:
            raise ValueError("trend_exit_buffer_points must not be negative")
        if exit_buffer_points < entry_buffer_points:
            raise ValueError(
                "trend_exit_buffer_points must be greater than or equal to "
                "trend_entry_buffer_points"
            )

        self.entry_buffer_points = entry_buffer_points
        self.exit_buffer_points = exit_buffer_points
        self.peaks: deque[DCEvent] = deque(maxlen=2)
        self.valleys: deque[DCEvent] = deque(maxlen=2)
        self.current_trend = "unknown"

    def update(self, event: DCEvent) -> str:
        if event.kind == "peak":
            self.peaks.append(event)
        elif event.kind == "valley":
            self.valleys.append(event)
        else:
            raise ValueError(f"unsupported pivot kind: {event.kind}")

        if len(self.peaks) < 2 or len(self.valleys) < 2:
            return self.current_trend

        self.current_trend = self._classify()
        return self.current_trend

    def _classify(self) -> str:
        previous_peak, latest_peak = self.peaks
        previous_valley, latest_valley = self.valleys
        peak_delta = latest_peak.price - previous_peak.price
        valley_delta = latest_valley.price - previous_valley.price

        peaks_rising_entry = peak_delta >= self.entry_buffer_points
        valleys_rising_entry = valley_delta >= self.entry_buffer_points
        peaks_falling_entry = peak_delta <= -self.entry_buffer_points
        valleys_falling_entry = valley_delta <= -self.entry_buffer_points

        peaks_rising_exit = peak_delta >= self.exit_buffer_points
        valleys_rising_exit = valley_delta >= self.exit_buffer_points
        peaks_falling_exit = peak_delta <= -self.exit_buffer_points
        valleys_falling_exit = valley_delta <= -self.exit_buffer_points

        if self.current_trend == "bull":
            if peaks_falling_exit and valleys_falling_exit:
                return "sideways"
            return "bull"

        if self.current_trend == "bear":
            if peaks_rising_exit and valleys_rising_exit:
                return "sideways"
            return "bear"

        bull_leg = peaks_rising_entry or valleys_rising_entry
        bear_leg = peaks_falling_entry or valleys_falling_entry
        if bull_leg and not bear_leg:
            return "bull"
        if bear_leg and not bull_leg:
            return "bear"
        return "sideways"


def add_dc_structure(
    frame: pd.DataFrame,
    *,
    threshold_points: float = 10,
    trend_buffer_points: float = 10,
    trend_mode: Literal["strict", "sticky"] = "strict",
    trend_entry_buffer_points: float | None = None,
    trend_exit_buffer_points: float | None = None,
    datetime_col: str = "datetime",
    price_col: str = "close",
) -> pd.DataFrame:
    """Append confirmed DC pivots and DC market-structure state to bars."""
    _validate_input(
        frame,
        threshold_points=threshold_points,
        trend_buffer_points=trend_buffer_points,
        trend_mode=trend_mode,
        trend_entry_buffer_points=trend_entry_buffer_points,
        trend_exit_buffer_points=trend_exit_buffer_points,
        datetime_col=datetime_col,
        price_col=price_col,
    )

    result = add_dc_pivots(
        frame,
        threshold_points=threshold_points,
        datetime_col=datetime_col,
        price_col=price_col,
    )
    result["dc_trend_state"] = "unknown"
    result["dc_is_bull"] = False
    result["dc_is_bear"] = False

    trend = _make_trend_classifier(
        trend_mode=trend_mode,
        trend_buffer_points=float(trend_buffer_points),
        trend_entry_buffer_points=trend_entry_buffer_points,
        trend_exit_buffer_points=trend_exit_buffer_points,
    )
    current_trend = "unknown"

    for row_number, (_, row) in enumerate(result.iterrows()):
        if pd.notna(row["dc_pivot_kind"]):
            event = DCEvent(
                kind=row["dc_pivot_kind"],
                price=float(row["dc_pivot_price"]),
                timestamp=row["dc_pivot_timestamp"],
                confirmation_price=float(row["dc_confirmation_price"]),
                confirmation_timestamp=row["dc_confirmation_timestamp"],
                tmv=float(row["dc_tmv"]),
                elapsed_minutes=float(row["dc_elapsed_minutes"]),
                r=float(row["dc_r"]),
            )
            current_trend = trend.update(event)

        _write_trend_state(result, row_number, current_trend)

    _append_recent_confirmed_pivots(result)
    return result


def add_dc_macd_comparison(
    frame: pd.DataFrame,
    *,
    threshold_points: float = 10,
    trend_buffer_points: float = 10,
    trend_mode: Literal["strict", "sticky"] = "strict",
    trend_entry_buffer_points: float | None = None,
    trend_exit_buffer_points: float | None = None,
    datetime_col: str = "datetime",
    price_col: str = "close",
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """Append close, confirmed-peak-line, and confirmed-valley-line MACD signals."""
    _validate_macd_periods(
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    result = add_dc_structure(
        frame,
        threshold_points=threshold_points,
        trend_buffer_points=trend_buffer_points,
        trend_mode=trend_mode,
        trend_entry_buffer_points=trend_entry_buffer_points,
        trend_exit_buffer_points=trend_exit_buffer_points,
        datetime_col=datetime_col,
        price_col=price_col,
    )

    peak_events = result["dc_pivot_kind"] == "peak"
    valley_events = result["dc_pivot_kind"] == "valley"
    pivot_prices = pd.to_numeric(result["dc_pivot_price"], errors="coerce")
    result["dc_peak_price_line"] = pivot_prices.where(peak_events).ffill()
    result["dc_valley_price_line"] = pivot_prices.where(valley_events).ffill()

    _append_macd_columns(
        result,
        prefix="close",
        source=result[price_col],
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    _append_macd_columns(
        result,
        prefix="dc_peak",
        source=result["dc_peak_price_line"],
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    _append_macd_columns(
        result,
        prefix="dc_valley",
        source=result["dc_valley_price_line"],
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    _append_macd_comparison_columns(result)
    return result


def _validate_input(
    frame: pd.DataFrame,
    *,
    threshold_points: float,
    trend_buffer_points: float,
    trend_mode: str,
    trend_entry_buffer_points: float | None,
    trend_exit_buffer_points: float | None,
    datetime_col: str,
    price_col: str,
) -> None:
    if threshold_points <= 0:
        raise ValueError("threshold_points must be positive")
    if trend_buffer_points < 0:
        raise ValueError("trend_buffer_points must not be negative")
    if trend_mode not in {"strict", "sticky"}:
        raise ValueError("trend_mode must be one of: strict, sticky")
    if trend_entry_buffer_points is not None and trend_entry_buffer_points < 0:
        raise ValueError("trend_entry_buffer_points must not be negative")
    if trend_exit_buffer_points is not None and trend_exit_buffer_points < 0:
        raise ValueError("trend_exit_buffer_points must not be negative")
    if trend_mode == "sticky":
        entry_buffer, exit_buffer = _resolve_sticky_buffers(
            trend_buffer_points=trend_buffer_points,
            trend_entry_buffer_points=trend_entry_buffer_points,
            trend_exit_buffer_points=trend_exit_buffer_points,
        )
        if exit_buffer < entry_buffer:
            raise ValueError(
                "trend_exit_buffer_points must be greater than or equal to "
                "trend_entry_buffer_points"
            )
    missing = [column for column in [datetime_col, price_col] if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")


def _make_trend_classifier(
    *,
    trend_mode: str,
    trend_buffer_points: float,
    trend_entry_buffer_points: float | None,
    trend_exit_buffer_points: float | None,
) -> _TrendClassifier | _StickyTrendClassifier:
    if trend_mode == "strict":
        return _TrendClassifier(trend_buffer_points)

    entry_buffer, exit_buffer = _resolve_sticky_buffers(
        trend_buffer_points=trend_buffer_points,
        trend_entry_buffer_points=trend_entry_buffer_points,
        trend_exit_buffer_points=trend_exit_buffer_points,
    )
    return _StickyTrendClassifier(entry_buffer, exit_buffer)


def _resolve_sticky_buffers(
    *,
    trend_buffer_points: float,
    trend_entry_buffer_points: float | None,
    trend_exit_buffer_points: float | None,
) -> tuple[float, float]:
    entry_buffer = (
        float(trend_buffer_points)
        if trend_entry_buffer_points is None
        else float(trend_entry_buffer_points)
    )
    exit_buffer = (
        float(trend_buffer_points)
        if trend_exit_buffer_points is None
        else float(trend_exit_buffer_points)
    )
    return entry_buffer, exit_buffer


def _validate_macd_periods(
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> None:
    if fast_period <= 0:
        raise ValueError("fast_period must be positive")
    if slow_period <= 0:
        raise ValueError("slow_period must be positive")
    if signal_period <= 0:
        raise ValueError("signal_period must be positive")
    if fast_period >= slow_period:
        raise ValueError("fast_period must be smaller than slow_period")


def _append_macd_columns(
    result: pd.DataFrame,
    *,
    prefix: str,
    source: pd.Series,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> None:
    numeric_source = pd.to_numeric(source, errors="coerce")
    fast_ema = numeric_source.ewm(span=fast_period, adjust=False).mean()
    slow_ema = numeric_source.ewm(span=slow_period, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=signal_period, adjust=False).mean()
    hist = macd - signal

    macd_col = f"{prefix}_macd"
    signal_col = f"{prefix}_macd_signal"
    hist_col = f"{prefix}_macd_hist"
    golden_col = f"{prefix}_macd_golden_cross"
    death_col = f"{prefix}_macd_death_cross"

    result[macd_col] = macd
    result[signal_col] = signal
    result[hist_col] = hist
    result[golden_col] = _crossed_above(macd, signal)
    result[death_col] = _crossed_below(macd, signal)


def _append_macd_comparison_columns(result: pd.DataFrame) -> None:
    result["dc_macd_structure"] = [
        _classify_peak_valley_macd(peak_macd, valley_macd)
        for peak_macd, valley_macd in zip(result["dc_peak_macd"], result["dc_valley_macd"])
    ]
    result["dc_macd_price_alignment"] = [
        _classify_price_alignment(close_macd, structure)
        for close_macd, structure in zip(result["close_macd"], result["dc_macd_structure"])
    ]

    for side_col, side_name in [("dc_is_bull", "bull"), ("dc_is_bear", "bear")]:
        side = result[side_col]
        for prefix in ["close", "dc_peak", "dc_valley"]:
            result[f"dc_{side_name}_{prefix}_macd_golden_cross"] = (
                side & result[f"{prefix}_macd_golden_cross"]
            )
            result[f"dc_{side_name}_{prefix}_macd_death_cross"] = (
                side & result[f"{prefix}_macd_death_cross"]
            )


def _append_recent_confirmed_pivots(result: pd.DataFrame) -> None:
    pivot_prices = pd.to_numeric(result["dc_pivot_price"], errors="coerce")
    peak_rows = result["dc_pivot_kind"] == "peak"
    valley_rows = result["dc_pivot_kind"] == "valley"

    result["dc_recent_peak_price"] = pivot_prices.where(peak_rows).ffill()
    result["dc_recent_peak_timestamp"] = result["dc_pivot_timestamp"].where(peak_rows).ffill()
    result["dc_recent_valley_price"] = pivot_prices.where(valley_rows).ffill()
    result["dc_recent_valley_timestamp"] = result["dc_pivot_timestamp"].where(valley_rows).ffill()


def _crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    current = left > right
    previous = left.shift(1) <= right.shift(1)
    valid = left.notna() & right.notna() & left.shift(1).notna() & right.shift(1).notna()
    return (current & previous & valid).fillna(False)


def _crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    current = left < right
    previous = left.shift(1) >= right.shift(1)
    valid = left.notna() & right.notna() & left.shift(1).notna() & right.shift(1).notna()
    return (current & previous & valid).fillna(False)


def _classify_peak_valley_macd(peak_macd: float, valley_macd: float) -> str:
    if pd.isna(peak_macd) or pd.isna(valley_macd):
        return "unknown"
    if peak_macd > 0 and valley_macd > 0:
        return "structure_bull"
    if peak_macd < 0 and valley_macd < 0:
        return "structure_bear"
    if peak_macd > 0 and valley_macd < 0:
        return "expanding_range"
    if peak_macd < 0 and valley_macd > 0:
        return "contracting_range"
    return "neutral"


def _classify_price_alignment(close_macd: float, structure: str) -> str:
    if pd.isna(close_macd) or structure == "unknown":
        return "unknown"
    if close_macd > 0 and structure == "structure_bull":
        return "bull_confirmed"
    if close_macd < 0 and structure == "structure_bear":
        return "bear_confirmed"
    if close_macd > 0 and structure == "structure_bear":
        return "price_bull_structure_bear"
    if close_macd < 0 and structure == "structure_bull":
        return "price_bear_structure_bull"
    if structure in {"expanding_range", "contracting_range"}:
        return structure
    return "mixed"


def _write_trend_state(result: pd.DataFrame, row_number: int, state: str) -> None:
    _set_cell(result, row_number, "dc_trend_state", state)
    _set_cell(result, row_number, "dc_is_bull", state == "bull")
    _set_cell(result, row_number, "dc_is_bear", state == "bear")


def _set_cell(result: pd.DataFrame, row_number: int, column: str, value: Any) -> None:
    result.iat[row_number, result.columns.get_loc(column)] = value
