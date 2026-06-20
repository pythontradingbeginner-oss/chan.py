from __future__ import annotations

import pandas as pd

from data_foundation.data.continuous_contract import build_continuous_contract
from data_foundation.data.continuous_contract import _assign_trading_day


def test_single_roll_back_adjusts_old_contract_history():
    frame = pd.DataFrame(
        [
            _row("2025-01-01", "A", 100, 200_000),
            _row("2025-01-01", "B", 110, 150_000),
            _row("2025-01-02", "B", 120, 220_000),
        ]
    )

    continuous, switch_log = build_continuous_contract(
        frame,
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert list(continuous["active_symbol"]) == ["A", "B"]
    assert list(continuous["close"]) == [110.0, 120.0]
    assert switch_log.iloc[0]["price_diff"] == 10.0


def test_multiple_rolls_accumulate_adjustments():
    frame = pd.DataFrame(
        [
            _row("2025-01-01", "A", 100, 220_000),
            _row("2025-01-01", "B", 110, 150_000),
            _row("2025-01-02", "B", 120, 230_000),
            _row("2025-01-02", "C", 125, 150_000),
            _row("2025-01-03", "C", 130, 240_000),
        ]
    )

    continuous, switch_log = build_continuous_contract(
        frame,
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert list(continuous["adjustment_points"]) == [15.0, 5.0, 0.0]
    assert list(continuous["close"]) == [115.0, 125.0, 130.0]
    assert list(switch_log["cumulative_adjustment"]) == [10.0, 15.0]


def test_no_adjust_mode_only_splices_raw_prices():
    frame = pd.DataFrame(
        [
            _row("2025-01-01", "A", 100, 200_000),
            _row("2025-01-01", "B", 110, 150_000),
            _row("2025-01-02", "B", 120, 220_000),
        ]
    )

    continuous, switch_log = build_continuous_contract(
        frame,
        method="no_adjust",
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert list(continuous["close"]) == [100.0, 120.0]
    assert list(continuous["adjustment_points"]) == [0.0, 0.0]
    assert switch_log.iloc[0]["cumulative_adjustment"] == 0.0


def test_switch_log_fields_are_complete():
    frame = pd.DataFrame(
        [
            _row("2025-01-01", "A", 100, 200_000),
            _row("2025-01-01", "B", 110, 150_000),
            _row("2025-01-02", "B", 120, 220_000),
        ]
    )

    _, switch_log = build_continuous_contract(frame, volume_threshold=100_000, cooldown_days=1)

    assert set(
        [
            "switch_date",
            "old_symbol",
            "new_symbol",
            "old_close",
            "new_close",
            "price_diff",
            "cumulative_adjustment",
            "basis_date",
        ]
    ).issubset(switch_log.columns)


def test_assign_trading_day_rolls_at_twenty_one_oclock():
    trading_days = _assign_trading_day(pd.Series(pd.to_datetime(["2025-01-02 20:59", "2025-01-02 21:00"])))

    assert list(trading_days) == [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")]


def test_one_minute_roll_logic_matches_replay_expected_adjustment():
    frame = pd.DataFrame(
        [
            _row("2025-01-01", "A", 100, 200_000),
            _row("2025-01-01", "B", 110, 150_000),
            _row("2025-01-02", "B", 120, 220_000),
            _row("2025-01-02", "C", 125, 150_000),
            _row("2025-01-03", "C", 130, 240_000),
        ]
    )

    continuous, _ = build_continuous_contract(frame, volume_threshold=100_000, cooldown_days=1)

    assert list(continuous["adjustment_points"]) == [15.0, 5.0, 0.0]


def _row(day: str, symbol: str, close: float, volume: float) -> dict:
    return {
        "symbol": symbol,
        "datetime": pd.Timestamp(f"{day} 15:00"),
        "trading_day": pd.Timestamp(day),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "open_interest": 1_000.0,
        "flags": 0,
    }
