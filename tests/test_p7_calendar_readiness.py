from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd
import pytest

from data_foundation import CalendarCoverageError, RBTradingCalendar
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy


class _Exchange(Enum):
    SHFE = "SHFE"


@dataclass
class _Bar:
    symbol: str
    datetime: datetime
    open_price: float = 3500
    high_price: float = 3501
    low_price: float = 3499
    close_price: float = 3500
    volume: float = 1
    turnover: float = 3500
    open_interest: float = 100
    exchange: _Exchange = _Exchange.SHFE
    gateway_name: str = "test"


@pytest.mark.parametrize(
    "holiday",
    [
        "2025-05-01",
        "2025-06-02",
        "2025-10-06",
        "2026-01-02",
        "2026-02-20",
        "2026-05-04",
        "2026-09-25",
        "2026-10-05",
    ],
)
def test_reviewed_shfe_holidays_are_closed(holiday: str) -> None:
    calendar = RBTradingCalendar.load_default()

    assert calendar.expected_minutes(holiday).empty


@pytest.mark.parametrize(
    "first_day",
    [
        "2025-04-07",
        "2025-05-06",
        "2025-06-03",
        "2025-10-09",
        "2026-01-05",
        "2026-02-24",
        "2026-04-07",
        "2026-05-06",
        "2026-06-22",
        "2026-09-28",
        "2026-10-08",
    ],
)
def test_first_day_after_holiday_has_no_preceding_night(first_day: str) -> None:
    minutes = RBTradingCalendar.load_default().expected_minutes(first_day)

    assert len(minutes) == 225
    assert "NIGHT" not in set(minutes["session"])


def test_normal_day_has_345_end_labelled_minutes_and_night_ownership() -> None:
    calendar = RBTradingCalendar.load_default()
    minutes = calendar.expected_minutes("2026-08-04")

    assert len(minutes) == 345
    mapped = calendar.map_datetimes(pd.Series([pd.Timestamp("2026-08-03 21:01")]))
    assert pd.Timestamp(mapped.iloc[0]["trading_day"]) == pd.Timestamp("2026-08-04")
    assert mapped.iloc[0]["session"] == "NIGHT"


def test_calendar_fails_closed_after_2026() -> None:
    calendar = RBTradingCalendar.load_default()

    with pytest.raises(CalendarCoverageError, match="calendar_out_of_range"):
        calendar.validate_coverage("2026-12-31", "2027-01-04")
    with pytest.raises(CalendarCoverageError, match="calendar_out_of_range"):
        calendar.map_datetimes(pd.Series([pd.Timestamp("2027-01-04 09:01")]))


def test_strategy_warmup_loader_uses_sqlite_and_100_days() -> None:
    calls = []
    sentinel = [_Bar("rb2610", datetime(2026, 8, 3, 21, 1))]

    class _Engine:
        def load_bar(self, *args):
            calls.append(args)
            return sentinel

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.load_days = 100
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.cta_engine = _Engine()
    strategy.write_log = lambda message: None
    strategy.on_bar = lambda bar: None

    result = strategy._load_warmup_bars()

    assert result == sentinel
    assert calls[0][1] == 100
    assert calls[0][-1] is True


def test_warmup_freshness_requires_latest_complete_session() -> None:
    calendar = RBTradingCalendar.load_default()
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._calendar = calendar
    strategy.vt_symbol = "rb2610.SHFE"
    now = datetime(2026, 8, 4, 4, 0)
    minutes = calendar.expected_minutes("2026-08-04")
    night = minutes[minutes["session"] == "NIGHT"]
    fresh_bars = [_Bar("rb2610", value.to_pydatetime()) for value in night["calendar_datetime"]]

    prepared, required, fresh = strategy._prepare_warmup_bars(fresh_bars, now)

    assert fresh
    assert required == pd.Timestamp("2026-08-03 23:00")
    assert len(prepared) == 120

    stale_minutes = calendar.expected_minutes("2026-07-13")
    stale_bars = [
        _Bar("rb2610", value.to_pydatetime())
        for value in stale_minutes["calendar_datetime"]
    ]
    _, _, stale = strategy._prepare_warmup_bars(stale_bars, now)
    assert not stale
