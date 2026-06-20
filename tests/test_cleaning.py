from __future__ import annotations

from zoneinfo import ZoneInfo
from datetime import date, datetime

import pandas as pd

from data_foundation.data.cleaning import (
    EXTREME_MOVE,
    LIMIT_DOWN,
    LIMIT_UP,
    clean_rb_1m_bars,
    detect_missing_minutes,
    get_rb_standard_minutes,
)


def test_ohlc_logic_removes_high_below_low_sample():
    cleaned, log = clean_rb_1m_bars(
        pd.DataFrame([_row("RB2501", "2025-01-02 09:01", 100, 99, 101, 100)])
    )

    assert cleaned.empty
    assert log.invalid_ohlc_removed == 1


def test_deduplication_keeps_last_symbol_datetime_row():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 100),
            _row("RB2501", "2025-01-02 09:01", 110, 111, 109, 110),
        ]
    )

    cleaned, log = clean_rb_1m_bars(frame)

    assert len(cleaned) == 1
    assert cleaned.iloc[0]["close"] == 110
    assert log.deduplicated == 1


def test_non_positive_price_is_removed():
    cleaned, log = clean_rb_1m_bars(
        pd.DataFrame([_row("RB2501", "2025-01-02 09:01", 100, 101, 99, 0)])
    )

    assert cleaned.empty
    assert log.invalid_price_removed == 1


def test_extreme_move_flag_is_set_without_removal():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 100),
            _row("RB2501", "2025-01-02 09:02", 120, 121, 119, 120),
        ]
    )

    cleaned, log = clean_rb_1m_bars(frame, extreme_return_threshold=0.05)

    assert len(cleaned) == 2
    assert int(cleaned.iloc[1]["flags"]) & EXTREME_MOVE
    assert log.extreme_moves_flagged == 1


def test_limit_up_day_is_flagged_from_previous_day_close():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 14:59", 100, 101, 99, 100),
            _row("RB2501", "2025-01-03 09:01", 110, 111, 109, 110),
        ]
    )

    cleaned, log = clean_rb_1m_bars(frame)

    assert int(cleaned.iloc[1]["flags"]) & LIMIT_UP
    assert log.limit_up_flagged == 1


def test_limit_down_day_is_flagged_from_previous_day_close():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 14:59", 100, 101, 99, 100),
            _row("RB2501", "2025-01-03 09:01", 90, 91, 89, 90),
        ]
    )

    cleaned, log = clean_rb_1m_bars(frame)

    assert int(cleaned.iloc[1]["flags"]) & LIMIT_DOWN
    assert log.limit_down_flagged == 1


def test_twenty_oclock_is_not_assigned_to_next_trading_day():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 20:59", 100, 101, 99, 100),
            _row("RB2501", "2025-01-02 21:00", 100, 101, 99, 100),
        ]
    )

    cleaned, _ = clean_rb_1m_bars(frame)

    assert cleaned.iloc[0]["trading_day"] == pd.Timestamp("2025-01-02")
    assert cleaned.iloc[1]["trading_day"] == pd.Timestamp("2025-01-03")


def test_datetime_values_use_zoneinfo_shanghai_timezone():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 100),
            _row("RB2501", pd.Timestamp("2025-01-02 01:02", tz="UTC"), 100, 101, 99, 100),
        ]
    )

    cleaned, _ = clean_rb_1m_bars(frame)

    assert isinstance(cleaned.iloc[0]["datetime"].tzinfo, ZoneInfo)
    assert cleaned.iloc[0]["datetime"].tzinfo.key == "Asia/Shanghai"
    assert cleaned.iloc[0]["datetime"] == pd.Timestamp("2025-01-02 09:01", tz=ZoneInfo("Asia/Shanghai"))
    assert cleaned.iloc[1]["datetime"] == pd.Timestamp("2025-01-02 09:02", tz=ZoneInfo("Asia/Shanghai"))


def test_missing_minutes_detects_expected_gap():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 100),
            _row("RB2501", "2025-01-02 09:03", 100, 101, 99, 100),
        ]
    )
    cleaned, log = clean_rb_1m_bars(frame)
    missing = detect_missing_minutes(cleaned[cleaned["symbol"] == "RB2501"])

    assert datetime(2025, 1, 2, 9, 2) in missing[date(2025, 1, 2)]
    assert log.missing_bars_per_day[date(2025, 1, 2)] > 0


def test_cleaning_log_counts_are_consistent():
    frame = pd.DataFrame(
        [
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 100),
            _row("RB2501", "2025-01-02 09:01", 100, 101, 99, 101),
            _row("RB2501", "2025-01-02 09:02", 0, 101, 99, 100),
        ]
    )

    cleaned, log = clean_rb_1m_bars(frame)

    assert log.raw_rows == 3
    assert log.deduplicated == 1
    assert log.invalid_price_removed == 1
    assert log.final_rows == len(cleaned) == 1


def test_standard_minutes_count_matches_rb_sessions():
    assert len(get_rb_standard_minutes(date(2025, 1, 2))) == 345
    assert get_rb_standard_minutes(date(2025, 1, 4)) == []


def _row(symbol: str, timestamp: str, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "symbol": symbol,
        "datetime": pd.Timestamp(timestamp),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
        "open_interest": 100.0,
    }
