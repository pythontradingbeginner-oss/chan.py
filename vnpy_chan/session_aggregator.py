from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import Any

import pandas as pd

from data_foundation.data.calendar import RBTradingCalendar


SUPPORTED_WINDOWS = {5, 15, 60}


class AggregationSequenceError(ValueError):
    """Hard failure for duplicate, out-of-order, or missing RB minutes."""

    def __init__(self, reason: str, timestamp: object, detail: str = "") -> None:
        self.reason = reason
        self.timestamp = pd.Timestamp(timestamp)
        message = f"{reason}: {self.timestamp}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AggregationAudit:
    frequency_minutes: int
    trading_day: pd.Timestamp | None
    session: str
    window_end: pd.Timestamp | None
    expected_count: int
    actual_count: int
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class _WindowInfo:
    trading_day: pd.Timestamp
    session: str
    window_id: int
    window_end: pd.Timestamp
    expected_count: int

    @property
    def key(self) -> tuple[pd.Timestamp, str, int]:
        return (self.trading_day, self.session, self.window_id)


@dataclass(slots=True)
class _BufferedBar:
    bar: Any
    timestamp: pd.Timestamp
    session_minute_index: int


class RbSessionBarAggregator:
    """Realtime RB N-minute aggregator that mirrors historical session rules."""

    def __init__(
        self,
        frequency_minutes: int,
        *,
        calendar: RBTradingCalendar | None = None,
    ) -> None:
        if frequency_minutes not in SUPPORTED_WINDOWS:
            supported = ", ".join(str(value) for value in sorted(SUPPORTED_WINDOWS))
            raise ValueError(
                f"frequency_minutes must be one of {supported}; got {frequency_minutes}"
            )
        self.frequency_minutes = int(frequency_minutes)
        self.calendar = calendar or RBTradingCalendar.load_default()
        self.audits: list[AggregationAudit] = []
        self._current_info: _WindowInfo | None = None
        self._buffer: list[_BufferedBar] = []
        self._window_cache: dict[tuple[pd.Timestamp, str, int], _WindowInfo] = {}
        self._last_timestamp: pd.Timestamp | None = None
        self._last_location: tuple[pd.Timestamp, str, int] | None = None
        self._faulted = False

    @property
    def last_audit(self) -> AggregationAudit | None:
        return self.audits[-1] if self.audits else None

    def update_bar(self, bar: Any) -> Any | None:
        """Consume one end-labelled 1m BarData-like object."""
        timestamp = _timestamp_naive(pd.Timestamp(bar.datetime))
        mapped = self.calendar.map_datetime(timestamp)
        if mapped is None:
            self._append_audit(
                AggregationAudit(
                    frequency_minutes=self.frequency_minutes,
                    trading_day=None,
                    session="",
                    window_end=None,
                    expected_count=0,
                    actual_count=0,
                    status="DROPPED",
                    reason="OUT_OF_SESSION",
                )
            )
            return None

        trading_day, session, session_minute_index = mapped
        if self._faulted:
            raise AggregationSequenceError(
                "AGGREGATOR_FAULTED",
                timestamp,
                "reset or reinitialize before accepting more bars",
            )
        self._validate_sequence(
            timestamp,
            trading_day,
            session,
            session_minute_index,
        )
        window_id = (session_minute_index - 1) // self.frequency_minutes
        info = self._window_info(trading_day, session, window_id)

        emitted: Any | None = None
        if self._current_info is not None and info.key != self._current_info.key:
            emitted = self._finalize_current()

        self._current_info = info
        self._buffer.append(
            _BufferedBar(
                bar=bar,
                timestamp=timestamp,
                session_minute_index=session_minute_index,
            )
        )
        self._last_timestamp = timestamp
        self._last_location = (trading_day, session, session_minute_index)
        if (
            info.expected_count <= self.frequency_minutes
            and _timestamp_naive(timestamp) >= info.window_end
        ):
            current = self._finalize_current()
            return emitted or current
        return emitted

    def flush(self) -> Any | None:
        """Finalize the in-flight window, useful at replay end."""
        return self._finalize_current()

    def reset(self) -> None:
        self._clear_window()
        self._last_timestamp = None
        self._last_location = None
        self._faulted = False

    def _clear_window(self) -> None:
        self._current_info = None
        self._buffer.clear()

    def _validate_sequence(
        self,
        timestamp: pd.Timestamp,
        trading_day: pd.Timestamp,
        session: str,
        session_minute_index: int,
    ) -> None:
        if self._last_timestamp is None or self._last_location is None:
            return
        if timestamp <= self._last_timestamp:
            reason = (
                "DUPLICATE_MINUTE"
                if timestamp == self._last_timestamp
                else "OUT_OF_ORDER_MINUTE"
            )
            self._raise_sequence_fault(reason, timestamp)

        last_day, last_session, last_index = self._last_location
        if (
            trading_day == last_day
            and session == last_session
            and session_minute_index == last_index + 1
        ):
            return

        expected = self.calendar.expected_minutes_between(last_day, trading_day)
        candidates = expected[
            expected["calendar_datetime"].map(_timestamp_naive) > self._last_timestamp
        ]
        if candidates.empty:
            self._raise_sequence_fault("MISSING_MINUTE", timestamp)
        expected_next = _timestamp_naive(
            pd.Timestamp(candidates["calendar_datetime"].min())
        )
        if timestamp != expected_next:
            self._raise_sequence_fault(
                "MISSING_MINUTE",
                timestamp,
                f"expected {expected_next}",
            )

    def _raise_sequence_fault(
        self,
        reason: str,
        timestamp: pd.Timestamp,
        detail: str = "",
    ) -> None:
        info = self._current_info
        self._append_audit(
            AggregationAudit(
                frequency_minutes=self.frequency_minutes,
                trading_day=info.trading_day if info is not None else None,
                session=info.session if info is not None else "",
                window_end=info.window_end if info is not None else None,
                expected_count=info.expected_count if info is not None else 0,
                actual_count=len(self._buffer),
                status="DROPPED",
                reason=reason,
            )
        )
        self._clear_window()
        self._faulted = True
        raise AggregationSequenceError(reason, timestamp, detail)

    def _finalize_current(self) -> Any | None:
        info = self._current_info
        if info is None:
            return None
        records = list(self._buffer)
        actual_count = len(records)
        unique_count = len({item.timestamp for item in records})
        status = "GENERATED"
        reason = ""
        if actual_count != self.frequency_minutes or unique_count != self.frequency_minutes:
            status = "DROPPED"
            reason = "MISSING_MINUTE"
        if actual_count != unique_count:
            status = "DROPPED"
            reason = "DUPLICATE_MINUTE"
        if info.expected_count < self.frequency_minutes:
            status = "DROPPED"
            reason = "SESSION_TAIL"
        if len({_bar_symbol(item.bar) for item in records}) > 1:
            raise ValueError(
                "ACTIVE_SYMBOL_INVARIANT_BROKEN: "
                f"{info.trading_day.date()} {info.session} ending {info.window_end}"
            )

        audit = AggregationAudit(
            frequency_minutes=self.frequency_minutes,
            trading_day=info.trading_day,
            session=info.session,
            window_end=info.window_end,
            expected_count=info.expected_count,
            actual_count=actual_count,
            status=status,
            reason=reason,
        )
        self._append_audit(audit)
        self._clear_window()
        if status != "GENERATED":
            if reason != "SESSION_TAIL":
                self._faulted = True
                raise AggregationSequenceError(reason, info.window_end)
            return None
        return _synthesize_bar(records, info.window_end)

    def _append_audit(self, audit: AggregationAudit) -> None:
        self.audits.append(audit)

    def _window_info(
        self,
        trading_day: pd.Timestamp,
        session: str,
        window_id: int,
    ) -> _WindowInfo:
        key = (trading_day, session, window_id)
        if key in self._window_cache:
            return self._window_cache[key]
        expected = self.calendar.expected_minutes(trading_day)
        expected = expected[expected["session"].astype(str) == session].copy()
        expected["window_id"] = (
            (expected["session_minute_index"].astype(int) - 1)
            // self.frequency_minutes
        )
        window = expected[expected["window_id"] == window_id]
        if window.empty:
            raise ValueError(
                f"missing RB session window: {trading_day.date()} {session} {window_id}"
            )
        info = _WindowInfo(
            trading_day=trading_day,
            session=session,
            window_id=window_id,
            window_end=pd.Timestamp(window["calendar_datetime"].max()),
            expected_count=int(len(window)),
        )
        self._window_cache[key] = info
        return info


def _synthesize_bar(records: list[_BufferedBar], window_end: pd.Timestamp) -> Any:
    first = records[0].bar
    last = records[-1].bar
    result = copy(last)
    result.datetime = _with_original_timezone(window_end, pd.Timestamp(last.datetime))
    result.open_price = float(first.open_price)
    result.high_price = max(float(item.bar.high_price) for item in records)
    result.low_price = min(float(item.bar.low_price) for item in records)
    result.close_price = float(last.close_price)
    result.volume = sum(float(getattr(item.bar, "volume", 0) or 0) for item in records)
    if hasattr(result, "turnover"):
        result.turnover = sum(
            float(getattr(item.bar, "turnover", 0) or 0) for item in records
        )
    if hasattr(result, "open_interest"):
        result.open_interest = float(getattr(last, "open_interest", 0) or 0)
    return result


def normalize_realtime_bar(bar: Any) -> Any:
    """Copy a VeighNa minute-start bar and convert it to RB end labelling."""
    result = copy(bar)
    original = pd.Timestamp(bar.datetime)
    normalized = original + pd.Timedelta(minutes=1)
    result.datetime = normalized.to_pydatetime()
    return result


def _bar_symbol(bar: Any) -> str:
    return str(getattr(bar, "symbol", ""))


def _timestamp_naive(value: pd.Timestamp) -> pd.Timestamp:
    if value.tzinfo is not None:
        return value.tz_convert("Asia/Shanghai").tz_localize(None)
    return value


def _with_original_timezone(value: pd.Timestamp, original: pd.Timestamp) -> Any:
    if original.tzinfo is None:
        return value.to_pydatetime()
    return value.tz_localize(original.tz).to_pydatetime()
