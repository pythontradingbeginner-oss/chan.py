from __future__ import annotations

import pandas as pd
import pytest

from data_foundation import aggregate_continuous_1m_to_5m, aggregate_continuous_1m_to_Nm
from data_foundation.data.bars import _infer_trading_day


def test_complete_five_rows_aggregate_to_one_5m_bar_with_ohlcv():
    frame = pd.DataFrame(
        [
            _row("2025-01-02 09:00", 100, 101, 99, 100, 1, 10),
            _row("2025-01-02 09:01", 101, 103, 100, 102, 2, 11),
            _row("2025-01-02 09:02", 102, 104, 101, 103, 3, 12),
            _row("2025-01-02 09:03", 103, 105, 102, 104, 4, 13),
            _row("2025-01-02 09:04", 104, 106, 103, 105, 5, 14),
        ]
    )

    result = aggregate_continuous_1m_to_5m(frame)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["datetime"] == pd.Timestamp("2025-01-02 09:00")
    assert row["open"] == 100
    assert row["high"] == 106
    assert row["low"] == 99
    assert row["close"] == 105
    assert row["volume"] == 15


def test_incomplete_window_is_dropped():
    frame = pd.DataFrame(
        [_row(f"2025-01-02 09:0{minute}", 100, 101, 99, 100, 1, 10) for minute in range(3)]
    )

    result = aggregate_continuous_1m_to_5m(frame)

    assert result.empty


def test_aggregation_does_not_cross_trading_day():
    rows = [
        _row(f"2025-01-02 14:5{minute}", 100, 101, 99, 100, 1, 10, "2025-01-02")
        for minute in range(7, 10)
    ]
    rows.extend(
        [
            _row("2025-01-02 21:00", 100, 101, 99, 100, 1, 10, "2025-01-03"),
            _row("2025-01-02 21:01", 100, 101, 99, 100, 1, 10, "2025-01-03"),
        ]
    )

    result = aggregate_continuous_1m_to_5m(pd.DataFrame(rows))

    assert result.empty


def test_infer_trading_day_rolls_at_twenty_one_oclock():
    trading_days = _infer_trading_day(pd.Series(pd.to_datetime(["2025-01-02 20:59", "2025-01-02 21:00"])))

    assert list(trading_days) == [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")]


def test_flags_are_merged_with_bitwise_or():
    frame = pd.DataFrame(
        [
            _row("2025-01-02 09:00", 100, 101, 99, 100, 1, 10, flags=1),
            _row("2025-01-02 09:01", 100, 101, 99, 100, 1, 10, flags=0),
            _row("2025-01-02 09:02", 100, 101, 99, 100, 1, 10, flags=4),
            _row("2025-01-02 09:03", 100, 101, 99, 100, 1, 10, flags=0),
            _row("2025-01-02 09:04", 100, 101, 99, 100, 1, 10, flags=0),
        ]
    )

    result = aggregate_continuous_1m_to_5m(frame)

    assert result.iloc[0]["flags"] == 5


def test_open_interest_and_active_symbol_take_last_value():
    frame = pd.DataFrame(
        [
            _row("2025-01-02 09:00", 100, 101, 99, 100, 1, 10, symbol="RB2501"),
            _row("2025-01-02 09:01", 100, 101, 99, 100, 1, 11, symbol="RB2501"),
            _row("2025-01-02 09:02", 100, 101, 99, 100, 1, 12, symbol="RB2501"),
            _row("2025-01-02 09:03", 100, 101, 99, 100, 1, 13, symbol="RB2505"),
            _row("2025-01-02 09:04", 100, 101, 99, 100, 1, 14, symbol="RB2505"),
        ]
    )

    result = aggregate_continuous_1m_to_5m(frame)

    assert result.iloc[0]["open_interest"] == 14
    assert result.iloc[0]["active_symbol"] == "RB2505"


def test_empty_input_returns_empty_frame():
    result = aggregate_continuous_1m_to_5m(pd.DataFrame(columns=_columns()))

    assert result.empty
    assert list(result.columns) == [
        "datetime",
        "trading_day",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "active_symbol",
        "flags",
    ]


def test_aggregate_15m_basic():
    closes = list(range(1, 31))
    datetimes = pd.date_range("2025-01-02 09:00", freq="1min", periods=30)
    df_1m = pd.DataFrame(
        {
            "datetime": datetimes,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100] * 30,
            "open_interest": [1000] * 30,
            "active_symbol": ["RB2501"] * 30,
            "flags": [0] * 30,
        }
    )

    result = aggregate_continuous_1m_to_Nm(df_1m, freq_minutes=15)

    assert len(result) == 2
    assert result.iloc[0]["close"] == 15
    assert result.iloc[1]["close"] == 30


def test_aggregate_invalid_freq():
    with pytest.raises(ValueError):
        aggregate_continuous_1m_to_Nm(pd.DataFrame(), freq_minutes=0)


def test_aggregation_does_not_mutate_input_dataframe():
    frame = pd.DataFrame(
        [_row(f"2025-01-02 09:0{minute}", 100, 101, 99, 100, 1, 10) for minute in range(5)]
    )
    original = frame.copy(deep=True)

    aggregate_continuous_1m_to_5m(frame)

    pd.testing.assert_frame_equal(frame, original)


def _row(
    timestamp: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    open_interest: float,
    trading_day: str = "2025-01-02",
    flags: int = 0,
    symbol: str = "RB2501",
) -> dict:
    return {
        "datetime": pd.Timestamp(timestamp),
        "trading_day": pd.Timestamp(trading_day),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "open_interest": open_interest,
        "active_symbol": symbol,
        "flags": flags,
    }


def _columns() -> list[str]:
    return [
        "datetime",
        "trading_day",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "active_symbol",
        "flags",
    ]
