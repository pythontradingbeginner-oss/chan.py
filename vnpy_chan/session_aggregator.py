from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import Any

import pandas as pd

from data_foundation.data.calendar import RBTradingCalendar


SUPPORTED_WINDOWS = {5, 15, 60}


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

    @property
    def last_audit(self) -> AggregationAudit | None:
        return self.audits[-1] if self.audits else None

    def update_bar(self, bar: Any) -> Any | None:
        """Consume one end-labelled 1m BarData-like object."""
        timestamp = pd.Timestamp(bar.datetime)
        mapped = self.calendar.map_datetimes(pd.Series([timestamp]))
        row = mapped.iloc[0]
        if pd.isna(row["trading_day"]):
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

        trading_day = pd.Timestamp(row["trading_day"]).normalize()
        session = str(row["session"])
        session_minute_index = int(row["session_minute_index"])
        window_id = (session_minute_index - 1) // self.frequency_minutes
        info = self._window_info(trading_day, session, window_id)

        emitted: Any | None = None
        if self._current_info is not None and info.key != self._current_info.key:
            emitted = self._finalize_current()

        self._current_info = info
        self._buffer.append(
            _BufferedBar(
                bar=bar,
                timestamp=_timestamp_naive(timestamp),
                session_minute_index=session_minute_index,
            )
        )
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
        self._current_info = None
        self._buffer.clear()

    def _finalize_current(self) -> Any | None:
        info = self._current_info
        if info is None:
            return None
        records = sorted(self._buffer, key=lambda item: item.session_minute_index)
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
        self.reset()
        if status != "GENERATED":
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
