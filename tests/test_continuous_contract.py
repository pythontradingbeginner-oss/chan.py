from __future__ import annotations

import pandas as pd

from data_foundation.data.continuous_contract import (
    _select_main_contract_days,
    build_continuous_contract,
    build_continuous_products,
)


def test_same_day_volume_cannot_change_same_day_contract():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "A", 105, 100_000),
            _row("2025-01-03", "B", 120, 900_000),
            _row("2025-01-06", "A", 106, 100_000),
            _row("2025-01-06", "B", 125, 200_000),
        ]
    )

    selected, _, rollovers = _select_main_contract_days(
        frame,
        min_daily_main_volume=100_000,
        rollover_cooldown_trading_days=1,
    )

    assert list(selected["trading_day"]) == [
        pd.Timestamp("2025-01-03"),
        pd.Timestamp("2025-01-06"),
    ]
    assert list(selected["symbol"]) == ["A", "B"]
    assert rollovers == [
        {
            "trading_day": pd.Timestamp("2025-01-06"),
            "decision_trading_day": pd.Timestamp("2025-01-03"),
            "from_contract_id": "A",
            "to_contract_id": "B",
        }
    ]


def test_first_day_is_dropped_and_weekend_uses_previous_available_day():
    frame = pd.DataFrame(
        [
            _row("2025-01-03", "A", 100, 200_000),
            _row("2025-01-03", "B", 110, 150_000),
            _row("2025-01-06", "A", 101, 120_000),
            _row("2025-01-06", "B", 111, 300_000),
        ]
    )

    selected, excluded, _ = _select_main_contract_days(
        frame,
        min_daily_main_volume=100_000,
        rollover_cooldown_trading_days=1,
    )

    assert list(selected["trading_day"]) == [pd.Timestamp("2025-01-06")]
    assert list(selected["decision_trading_day"]) == [pd.Timestamp("2025-01-03")]
    assert list(selected["symbol"]) == ["A"]
    assert excluded[0] == {
        "trading_day": pd.Timestamp("2025-01-03").date(),
        "reason": "no_prior_decision_day",
    }


def test_tied_decision_day_volume_selects_symbol_deterministically():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 200_000),
            _row("2025-01-03", "A", 101, 200_000),
            _row("2025-01-03", "B", 111, 200_000),
        ]
    )

    selected, _, _ = _select_main_contract_days(
        frame,
        min_daily_main_volume=100_000,
        rollover_cooldown_trading_days=1,
    )

    assert list(selected["symbol"]) == ["A"]


def test_selected_contract_missing_excludes_day_without_same_day_fallback():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "B", 120, 300_000),
            _row("2025-01-06", "B", 125, 300_000),
        ]
    )

    selected, excluded, _ = _select_main_contract_days(
        frame,
        min_daily_main_volume=100_000,
        rollover_cooldown_trading_days=1,
    )

    assert list(selected["trading_day"]) == [pd.Timestamp("2025-01-06")]
    assert list(selected["symbol"]) == ["B"]
    assert any(
        item["trading_day"] == pd.Timestamp("2025-01-03").date()
        and item["reason"] == "selected_contract_missing"
        for item in excluded
    )


def test_single_roll_back_adjusts_old_history_on_effective_day():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "A", 105, 100_000),
            _row("2025-01-03", "B", 120, 220_000),
            _row("2025-01-06", "B", 130, 230_000),
        ]
    )

    continuous, switch_log = build_continuous_contract(
        frame,
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert list(continuous["active_symbol"]) == ["A", "B"]
    assert list(continuous["close"]) == [120.0, 130.0]
    assert switch_log.iloc[0]["decision_date"] == pd.Timestamp("2025-01-03").date()
    assert switch_log.iloc[0]["switch_date"] == pd.Timestamp("2025-01-06").date()
    assert switch_log.iloc[0]["price_diff"] == 15.0


def test_no_adjust_mode_uses_causal_selection_without_price_adjustment():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "A", 105, 100_000),
            _row("2025-01-03", "B", 120, 220_000),
            _row("2025-01-06", "B", 130, 230_000),
        ]
    )

    continuous, switch_log = build_continuous_contract(
        frame,
        method="no_adjust",
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert list(continuous["close"]) == [105.0, 130.0]
    assert list(continuous["adjustment_points"]) == [0.0, 0.0]
    assert switch_log.iloc[0]["cumulative_adjustment"] == 0.0


def test_dual_products_share_selection_and_non_price_fields():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "A", 105, 100_000),
            _row("2025-01-03", "B", 120, 220_000),
            _row("2025-01-06", "B", 130, 230_000),
        ]
    )
    raw, adjusted, switches, decisions = build_continuous_products(
        frame, volume_threshold=100_000, cooldown_days=1
    )

    columns = ["datetime", "trading_day", "volume", "open_interest", "active_symbol", "flags"]
    pd.testing.assert_frame_equal(raw[columns], adjusted[columns])
    assert list(raw["close"]) == list(raw["raw_close"])
    assert list(adjusted["close"]) != list(raw["close"])
    assert not switches.empty
    assert not decisions.empty


def test_switch_log_fields_include_decision_and_effective_dates():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-02", "B", 110, 150_000),
            _row("2025-01-03", "A", 105, 100_000),
            _row("2025-01-03", "B", 120, 220_000),
            _row("2025-01-06", "B", 130, 230_000),
        ]
    )

    _, switch_log = build_continuous_contract(
        frame,
        volume_threshold=100_000,
        cooldown_days=1,
    )

    assert {
        "switch_date",
        "decision_date",
        "old_symbol",
        "new_symbol",
        "old_close",
        "new_close",
        "price_diff",
        "cumulative_adjustment",
        "basis_date",
    }.issubset(switch_log.columns)


def test_single_trading_day_returns_empty_frames():
    frame = pd.DataFrame([_row("2025-01-02", "A", 100, 200_000)])

    continuous, switch_log = build_continuous_contract(frame)

    assert continuous.empty
    assert switch_log.empty
    assert "decision_date" in switch_log.columns


def test_build_does_not_mutate_input():
    frame = pd.DataFrame(
        [
            _row("2025-01-02", "A", 100, 200_000),
            _row("2025-01-03", "A", 101, 200_000),
        ]
    )
    original = frame.copy(deep=True)

    build_continuous_contract(frame)

    pd.testing.assert_frame_equal(frame, original)


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
