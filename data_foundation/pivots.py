from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd


DC_PIVOT_COLUMNS = [
    "dc_pivot_kind",
    "dc_pivot_price",
    "dc_pivot_timestamp",
    "dc_confirmation_price",
    "dc_confirmation_timestamp",
    "dc_tmv",
    "dc_elapsed_minutes",
    "dc_r",
]

DC_PIVOT_EVENT_COLUMNS = [*DC_PIVOT_COLUMNS, "dc_confirmation_row"]


@dataclass
class DCEvent:
    """Confirmed peak or valley and the later bar that confirmed it."""

    kind: str
    price: float
    timestamp: Any
    confirmation_price: float
    confirmation_timestamp: Any
    tmv: float
    elapsed_minutes: float
    r: float


@dataclass
class DCSnapshot:
    """Live, not-yet-confirmed directional-change state."""

    direction: int = 0
    extreme_price: float = 0
    start_price: float = 0
    tmv: float = 0
    elapsed_minutes: float = 0
    r: float = 0


class DirectionalChangeDetector:
    """Confirm close-price peaks and valleys after a fixed points reversal."""

    def __init__(self, threshold_points: float) -> None:
        if threshold_points <= 0:
            raise ValueError("threshold_points must be positive")

        self.threshold_points = threshold_points
        self.direction = 0
        self.initial_price = 0.0
        self.initial_time: Any = None
        self.last_pivot_price = 0.0
        self.last_pivot_time: Any = None
        self.extreme_price = 0.0
        self.extreme_time: Any = None
        self.last_event: DCEvent | None = None

    def reset(self) -> None:
        self.direction = 0
        self.initial_price = 0.0
        self.initial_time = None
        self.last_pivot_price = 0.0
        self.last_pivot_time = None
        self.extreme_price = 0.0
        self.extreme_time = None
        self.last_event = None

    def update(self, price: float, timestamp: Any) -> DCEvent | None:
        """Process one completed close price."""
        if price <= 0:
            return None

        if not self.initial_price:
            self.initial_price = price
            self.initial_time = timestamp
            self.extreme_price = price
            self.extreme_time = timestamp
            return None

        if self.direction == 0:
            event = self._initialize(price, timestamp)
        elif self.direction > 0:
            event = self._update_candidate_peak(price, timestamp)
        else:
            event = self._update_candidate_valley(price, timestamp)

        if event:
            self.last_event = event

        return event

    def snapshot(self, timestamp: Any) -> DCSnapshot:
        """Return the current unfinished movement for live strategy use."""
        if not self.extreme_price or self.extreme_time is None:
            return DCSnapshot()

        if not self.last_pivot_price or self.last_pivot_time is None:
            start_price = self.initial_price
            start_time = self.initial_time
        else:
            start_price = self.last_pivot_price
            start_time = self.last_pivot_time

        if not start_price or start_time is None:
            return DCSnapshot()

        elapsed = _elapsed_minutes(start_time, timestamp)
        tmv = abs(self.extreme_price - start_price) / self.threshold_points
        return DCSnapshot(
            direction=self.direction,
            extreme_price=self.extreme_price,
            start_price=start_price,
            tmv=tmv,
            elapsed_minutes=elapsed,
            r=tmv / elapsed if elapsed else 0.0,
        )

    def _initialize(self, price: float, timestamp: Any) -> DCEvent | None:
        if price >= self.initial_price + self.threshold_points:
            event = self._make_event(
                "valley",
                self.initial_price,
                self.initial_time,
                price,
                timestamp,
            )
            self._start_tracking_peak(price, timestamp)
            return event

        if price <= self.initial_price - self.threshold_points:
            event = self._make_event(
                "peak",
                self.initial_price,
                self.initial_time,
                price,
                timestamp,
            )
            self._start_tracking_valley(price, timestamp)
            return event

        return None

    def _update_candidate_peak(self, price: float, timestamp: Any) -> DCEvent | None:
        if price > self.extreme_price:
            self.extreme_price = price
            self.extreme_time = timestamp
            return None

        if price <= self.extreme_price - self.threshold_points:
            event = self._make_event(
                "peak",
                self.extreme_price,
                self.extreme_time,
                price,
                timestamp,
            )
            self._start_tracking_valley(price, timestamp)
            return event

        return None

    def _update_candidate_valley(self, price: float, timestamp: Any) -> DCEvent | None:
        if price < self.extreme_price:
            self.extreme_price = price
            self.extreme_time = timestamp
            return None

        if price >= self.extreme_price + self.threshold_points:
            event = self._make_event(
                "valley",
                self.extreme_price,
                self.extreme_time,
                price,
                timestamp,
            )
            self._start_tracking_peak(price, timestamp)
            return event

        return None

    def _make_event(
        self,
        kind: str,
        pivot_price: float,
        pivot_time: Any,
        confirmation_price: float,
        confirmation_time: Any,
    ) -> DCEvent:
        if pivot_time is None:
            raise ValueError("pivot_time is required")

        if self.last_pivot_price and self.last_pivot_time is not None:
            tmv = abs(pivot_price - self.last_pivot_price) / self.threshold_points
            elapsed = _elapsed_minutes(self.last_pivot_time, pivot_time)
        else:
            tmv = 0.0
            elapsed = 0.0

        event = DCEvent(
            kind=kind,
            price=pivot_price,
            timestamp=pivot_time,
            confirmation_price=confirmation_price,
            confirmation_timestamp=confirmation_time,
            tmv=tmv,
            elapsed_minutes=elapsed,
            r=tmv / elapsed if elapsed else 0.0,
        )

        self.last_pivot_price = pivot_price
        self.last_pivot_time = pivot_time
        return event

    def _start_tracking_peak(self, price: float, timestamp: Any) -> None:
        self.direction = 1
        self.extreme_price = price
        self.extreme_time = timestamp

    def _start_tracking_valley(self, price: float, timestamp: Any) -> None:
        self.direction = -1
        self.extreme_price = price
        self.extreme_time = timestamp


def detect_dc_pivots(
    frame: pd.DataFrame,
    *,
    threshold_points: float = 10,
    datetime_col: str = "datetime",
    price_col: str = "close",
) -> pd.DataFrame:
    """Return confirmed DC peak and valley events from a bar DataFrame."""
    _validate_pivot_input(
        frame,
        threshold_points=threshold_points,
        datetime_col=datetime_col,
        price_col=price_col,
    )

    detector = DirectionalChangeDetector(float(threshold_points))
    timestamps = pd.to_datetime(frame[datetime_col])
    rows: list[dict[str, Any]] = []

    for row_number, (_, row) in enumerate(frame.iterrows()):
        price_value = row[price_col]
        timestamp = timestamps.iloc[row_number]

        if pd.isna(price_value) or pd.isna(timestamp):
            continue

        event = detector.update(float(price_value), timestamp)
        if event is None:
            continue

        rows.append(
            {
                "dc_pivot_kind": event.kind,
                "dc_pivot_price": event.price,
                "dc_pivot_timestamp": event.timestamp,
                "dc_confirmation_price": event.confirmation_price,
                "dc_confirmation_timestamp": event.confirmation_timestamp,
                "dc_tmv": event.tmv,
                "dc_elapsed_minutes": event.elapsed_minutes,
                "dc_r": event.r,
                "dc_confirmation_row": row_number,
            }
        )

    return pd.DataFrame(rows, columns=DC_PIVOT_EVENT_COLUMNS)


def add_dc_pivots(
    frame: pd.DataFrame,
    *,
    threshold_points: float = 10,
    datetime_col: str = "datetime",
    price_col: str = "close",
) -> pd.DataFrame:
    """Append confirmed DC peak and valley event columns to bar rows."""
    events = detect_dc_pivots(
        frame,
        threshold_points=threshold_points,
        datetime_col=datetime_col,
        price_col=price_col,
    )

    result = frame.copy(deep=True)
    _initialize_pivot_columns(result)

    for event in events.itertuples(index=False):
        row_number = int(event.dc_confirmation_row)
        _set_cell(result, row_number, "dc_pivot_kind", event.dc_pivot_kind)
        _set_cell(result, row_number, "dc_pivot_price", event.dc_pivot_price)
        _set_cell(result, row_number, "dc_pivot_timestamp", event.dc_pivot_timestamp)
        _set_cell(result, row_number, "dc_confirmation_price", event.dc_confirmation_price)
        _set_cell(
            result,
            row_number,
            "dc_confirmation_timestamp",
            event.dc_confirmation_timestamp,
        )
        _set_cell(result, row_number, "dc_tmv", event.dc_tmv)
        _set_cell(result, row_number, "dc_elapsed_minutes", event.dc_elapsed_minutes)
        _set_cell(result, row_number, "dc_r", event.dc_r)

    return result


def _initialize_pivot_columns(result: pd.DataFrame) -> None:
    result["dc_pivot_kind"] = None
    result["dc_pivot_price"] = pd.NA
    result["dc_pivot_timestamp"] = None
    result["dc_confirmation_price"] = pd.NA
    result["dc_confirmation_timestamp"] = None
    result["dc_tmv"] = pd.NA
    result["dc_elapsed_minutes"] = pd.NA
    result["dc_r"] = pd.NA


def _validate_pivot_input(
    frame: pd.DataFrame,
    *,
    threshold_points: float,
    datetime_col: str,
    price_col: str,
) -> None:
    if threshold_points <= 0:
        raise ValueError("threshold_points must be positive")
    missing = [column for column in [datetime_col, price_col] if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")


def _set_cell(result: pd.DataFrame, row_number: int, column: str, value: Any) -> None:
    result.iat[row_number, result.columns.get_loc(column)] = value


def _elapsed_minutes(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds() / 60, 0.0)
