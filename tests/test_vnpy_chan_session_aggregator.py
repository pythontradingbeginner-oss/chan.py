from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd
import pytest

from data_foundation import RBTradingCalendar, aggregate_continuous_1m_to_Nm
from vnpy_chan.converter import bars_to_ohlc_frame
from vnpy_chan.session_aggregator import (
    AggregationSequenceError,
    RbSessionBarAggregator,
    normalize_realtime_bar,
)


class _Exchange(Enum):
    SHFE = "SHFE"


@dataclass
class _Bar:
    symbol: str
    datetime: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float = 1
    turnover: float = 0
    open_interest: float = 1
    exchange: _Exchange = _Exchange.SHFE
    gateway_name: str = "test"


@pytest.mark.parametrize("frequency", [5, 15, 60])
def test_live_rb_session_aggregator_matches_historical_replay(frequency: int) -> None:
    calendar = RBTradingCalendar.load_default()
    minutes = calendar.expected_minutes(pd.Timestamp("2025-01-03"))
    frame = pd.DataFrame(
        [
            _row(timestamp, index)
            for index, timestamp in enumerate(minutes["calendar_datetime"], start=1)
        ]
    )
    expected = aggregate_continuous_1m_to_Nm(frame, frequency)

    aggregator = RbSessionBarAggregator(frequency, calendar=calendar)
    emitted = []
    for bar in _bars_from_frame(frame):
        result = aggregator.update_bar(bar)
        if result is not None:
            emitted.append(result)
    final = aggregator.flush()
    if final is not None:
        emitted.append(final)

    actual = bars_to_ohlc_frame(emitted)
    assert list(actual["datetime"]) == list(expected["datetime"])
    for column in ["open", "high", "low", "close", "volume", "open_interest"]:
        assert list(actual[column]) == list(expected[column])


def test_live_60m_aggregator_drops_session_tail_instead_of_crossing_break() -> None:
    rows = []
    rows.extend(
        _row(timestamp, index)
        for index, timestamp in enumerate(
            pd.date_range("2025-01-02 10:01", "2025-01-02 10:15", freq="1min"),
            start=1,
        )
    )
    rows.extend(
        _row(timestamp, index)
        for index, timestamp in enumerate(
            pd.date_range("2025-01-02 10:31", "2025-01-02 11:30", freq="1min"),
            start=100,
        )
    )

    aggregator = RbSessionBarAggregator(60)
    emitted = [
        result
        for bar in _bars_from_frame(pd.DataFrame(rows))
        if (result := aggregator.update_bar(bar)) is not None
    ]

    assert [bar.datetime for bar in emitted] == [datetime(2025, 1, 2, 11, 30)]
    assert aggregator.audits[0].reason == "SESSION_TAIL"


def test_live_aggregator_fails_fast_on_active_symbol_change_inside_window() -> None:
    frame = pd.DataFrame(
        [_row(f"2025-01-02 09:0{minute}", minute) for minute in range(1, 6)]
    )
    bars = list(_bars_from_frame(frame))
    bars[-1].symbol = "RB2505"
    aggregator = RbSessionBarAggregator(5)

    with pytest.raises(ValueError, match="ACTIVE_SYMBOL_INVARIANT_BROKEN"):
        for bar in bars:
            aggregator.update_bar(bar)


def test_realtime_minute_start_label_is_copied_and_shifted_to_end() -> None:
    source = next(
        _bars_from_frame(pd.DataFrame([_row("2026-08-04 09:00", 1)]))
    )

    normalized = normalize_realtime_bar(source)

    assert source.datetime == datetime(2026, 8, 4, 9, 0)
    assert normalized is not source
    assert normalized.datetime == datetime(2026, 8, 4, 9, 1)


@pytest.mark.parametrize(
    ("timestamps", "reason"),
    [
        (["2026-08-04 09:01", "2026-08-04 09:01"], "DUPLICATE_MINUTE"),
        (["2026-08-04 09:02", "2026-08-04 09:01"], "OUT_OF_ORDER_MINUTE"),
        (["2026-08-04 09:01", "2026-08-04 09:03"], "MISSING_MINUTE"),
    ],
)
def test_live_aggregator_hard_fails_invalid_sequence(timestamps, reason) -> None:
    aggregator = RbSessionBarAggregator(5)
    bars = _bars_from_frame(
        pd.DataFrame([_row(timestamp, index) for index, timestamp in enumerate(timestamps)])
    )

    with pytest.raises(AggregationSequenceError, match=reason):
        for bar in bars:
            aggregator.update_bar(bar)


def test_live_aggregator_accepts_reviewed_session_break() -> None:
    aggregator = RbSessionBarAggregator(5)
    frame = pd.DataFrame(
        [
            *[_row(value, index) for index, value in enumerate(pd.date_range("2026-08-04 10:11", "2026-08-04 10:15", freq="min"))],
            *[_row(value, index) for index, value in enumerate(pd.date_range("2026-08-04 10:31", "2026-08-04 10:35", freq="min"), start=10)],
        ]
    )

    emitted = [
        result
        for bar in _bars_from_frame(frame)
        if (result := aggregator.update_bar(bar)) is not None
    ]

    assert [bar.datetime for bar in emitted] == [
        datetime(2026, 8, 4, 10, 15),
        datetime(2026, 8, 4, 10, 35),
    ]


def _bars_from_frame(frame: pd.DataFrame):
    for row in frame.itertuples(index=False):
        yield _Bar(
            symbol=str(row.active_symbol),
            datetime=pd.Timestamp(row.datetime).to_pydatetime(),
            open_price=float(row.open),
            high_price=float(row.high),
            low_price=float(row.low),
            close_price=float(row.close),
            volume=float(row.volume),
            open_interest=float(row.open_interest),
        )


def _row(timestamp: object, value: float) -> dict:
    return {
        "datetime": pd.Timestamp(timestamp),
        "open": float(value),
        "high": float(value) + 1,
        "low": float(value) - 1,
        "close": float(value),
        "volume": 1.0,
        "open_interest": float(value),
        "active_symbol": "RB2501",
        "flags": 0,
    }
