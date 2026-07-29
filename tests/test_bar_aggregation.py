from __future__ import annotations

import pandas as pd
import pytest

from data_foundation import aggregate_continuous_1m_to_5m, aggregate_continuous_1m_to_Nm


def test_end_labelled_minutes_form_end_labelled_bar():
    frame = pd.DataFrame(
        [_row(f"2025-01-02 09:0{minute}", minute) for minute in range(1, 6)]
    )
    result = aggregate_continuous_1m_to_5m(frame)

    assert len(result) == 1
    assert result.iloc[0]["datetime"] == pd.Timestamp("2025-01-02 09:05")
    assert result.iloc[0]["open"] == 1
    assert result.iloc[0]["close"] == 5
    assert result.iloc[0]["volume"] == 5


def test_incomplete_window_is_dropped_and_audited():
    frame = pd.DataFrame([_row("2025-01-02 09:01", 1), _row("2025-01-02 09:03", 3)])
    result, audit = aggregate_continuous_1m_to_5m(frame, return_audit=True)

    assert result.empty
    target = audit[audit["window_end"] == pd.Timestamp("2025-01-02 09:05")]
    assert target.iloc[0]["reason"] == "MISSING_MINUTE"


def test_windows_do_not_cross_session_break():
    times = list(pd.date_range("2025-01-02 10:11", "2025-01-02 10:15", freq="1min"))
    times += list(pd.date_range("2025-01-02 10:31", "2025-01-02 10:35", freq="1min"))
    result = aggregate_continuous_1m_to_5m(
        pd.DataFrame([_row(str(value), index) for index, value in enumerate(times)])
    )

    assert list(result["datetime"]) == [pd.Timestamp("2025-01-02 10:15"), pd.Timestamp("2025-01-02 10:35")]


def test_flags_are_merged_and_last_open_interest_is_used():
    rows = [_row(f"2025-01-02 09:0{minute}", minute) for minute in range(1, 6)]
    rows[0]["flags"] = 1
    rows[2]["flags"] = 4
    rows[-1]["open_interest"] = 999
    result = aggregate_continuous_1m_to_5m(pd.DataFrame(rows))

    assert result.iloc[0]["flags"] == 5
    assert result.iloc[0]["open_interest"] == 999


def test_multiple_active_symbols_fail_fast():
    rows = [_row(f"2025-01-02 09:0{minute}", minute) for minute in range(1, 6)]
    rows[-1]["active_symbol"] = "RB2505"
    with pytest.raises(ValueError, match="ACTIVE_SYMBOL_INVARIANT_BROKEN"):
        aggregate_continuous_1m_to_5m(pd.DataFrame(rows))


@pytest.mark.parametrize("frequency,expected", [(5, 69), (15, 23), (30, 11), (60, 5)])
def test_complete_normal_day_has_expected_bar_count(frequency, expected):
    from data_foundation import RBTradingCalendar

    minutes = RBTradingCalendar.load_default().expected_minutes(pd.Timestamp("2025-01-03"))
    frame = pd.DataFrame([_row(str(value), index) for index, value in enumerate(minutes["calendar_datetime"])])
    result = aggregate_continuous_1m_to_Nm(frame, frequency)

    assert len(result) == expected


def test_invalid_frequency_is_rejected():
    with pytest.raises(ValueError):
        aggregate_continuous_1m_to_Nm(pd.DataFrame(), 7)


def test_bars_and_audit_are_sorted_by_actual_time_with_timezone():
    times = list(pd.date_range("2025-01-03 09:01", periods=5, freq="1min"))
    times += list(pd.date_range("2025-01-02 21:01", periods=5, freq="1min"))
    frame = pd.DataFrame([_row(str(value), index) for index, value in enumerate(times)])
    frame["datetime"] = frame["datetime"].dt.tz_localize("Asia/Shanghai")
    bars, audit = aggregate_continuous_1m_to_Nm(frame, 5, return_audit=True)
    assert bars["datetime"].is_monotonic_increasing
    assert audit["window_end"].is_monotonic_increasing
    assert str(audit["window_end"].dt.tz) == "Asia/Shanghai"


def test_empty_input_and_no_mutation():
    empty = pd.DataFrame(columns=_columns())
    assert aggregate_continuous_1m_to_5m(empty).empty
    frame = pd.DataFrame([_row(f"2025-01-02 09:0{minute}", minute) for minute in range(1, 6)])
    original = frame.copy(deep=True)
    aggregate_continuous_1m_to_5m(frame)
    pd.testing.assert_frame_equal(frame, original)


def _row(timestamp: str, value: float) -> dict:
    return {
        "datetime": pd.Timestamp(timestamp),
        "open": value,
        "high": value + 1,
        "low": value - 1,
        "close": value,
        "volume": 1,
        "open_interest": value,
        "active_symbol": "RB2501",
        "flags": 0,
    }


def _columns() -> list[str]:
    return [
        "datetime", "open", "high", "low", "close", "volume",
        "open_interest", "active_symbol", "flags",
    ]
