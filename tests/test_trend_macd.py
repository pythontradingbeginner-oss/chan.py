from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_foundation import add_trend_macd, aggregate_continuous_1m_to_Nm
from data_foundation.features import add_trend_macd as add_trend_macd_from_features


START = datetime(2026, 1, 1, 9, 0)


def test_can_import_from_top_level_and_features_subpackage():
    assert add_trend_macd is add_trend_macd_from_features


def test_fixed_threshold_marks_body_above_or_equal_x_as_trend_bar():
    result = add_trend_macd(
        _bars(
            [
                (100, 104, 105, 99, 104),
                (104, 109, 110, 103, 109),
                (109, 112, 113, 108, 112),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
    )

    assert list(result["trend_body"]) == [4, 5, 3]
    assert list(result["trend_move"]) == [4, 5, 3]
    assert list(result["trend_move_mode"].unique()) == ["body_or_close_to_previous_close"]
    assert list(result["trend_is_bar"]) == [False, True, False]
    assert list(result["trend_direction"]) == [0, 1, 0]


def test_close_to_last_trend_mode_uses_previous_close_until_first_trend():
    result = add_trend_macd(
        _bars(
            [
                (100, 100, 100, 100, 100),
                (101, 104, 105, 100, 104),
                (104, 106, 107, 103, 106),
                (106, 112, 113, 105, 112),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
        move_mode="close_to_last_trend",
    )

    assert pd.isna(result.loc[0, "trend_move"])
    assert pd.isna(result.loc[0, "trend_reference_close"])
    assert result.loc[1, "trend_reference_close"] == 100
    assert result.loc[1, "trend_move"] == 4
    assert result.loc[2, "trend_reference_close"] == 104
    assert result.loc[2, "trend_move"] == 2
    assert result.loc[3, "trend_reference_close"] == 106
    assert result.loc[3, "trend_move"] == 6
    assert list(result["trend_is_bar"]) == [False, False, False, True]


def test_close_to_last_trend_mode_uses_last_trend_close_after_first_trend():
    result = add_trend_macd(
        _bars(
            [
                (100, 100, 100, 100, 100),
                (100, 106, 107, 99, 106),
                (106, 108, 109, 105, 108),
                (111, 112, 113, 110, 112),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
        move_mode="close_to_last_trend",
    )

    assert list(result["trend_body"]) == [0, 6, 2, 1]
    assert pd.isna(result.loc[0, "trend_move"])
    assert result.loc[1, "trend_reference_close"] == 100
    assert result.loc[1, "trend_is_bar"] == True
    assert result.loc[2, "trend_reference_close"] == 106
    assert result.loc[2, "trend_move"] == 2
    assert result.loc[2, "trend_is_bar"] == False
    assert result.loc[3, "trend_reference_close"] == 106
    assert result.loc[3, "trend_move"] == 6
    assert result.loc[3, "trend_is_bar"] == True
    assert result.loc[3, "trend_direction"] == 1


def test_body_or_previous_close_mode_captures_gap_like_move_with_small_body():
    result = add_trend_macd(
        _bars(
            [
                (100, 100, 100, 100, 100),
                (110, 111, 112, 109, 111),
                (111, 113, 114, 110, 113),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
    )

    assert list(result["trend_body"]) == [0, 1, 2]
    assert result.loc[0, "trend_move"] == 0
    assert result.loc[0, "trend_reference_close"] == 100
    assert result.loc[0, "trend_is_bar"] == False
    assert result.loc[1, "trend_reference_close"] == 100
    assert result.loc[1, "trend_move"] == 11
    assert result.loc[1, "trend_is_bar"] == True
    assert result.loc[1, "trend_direction"] == 1
    assert result.loc[2, "trend_reference_close"] == 111
    assert result.loc[2, "trend_move"] == 2
    assert result.loc[2, "trend_is_bar"] == False


def test_quantile_threshold_uses_only_prior_bodies():
    result = add_trend_macd(
        _bars(
            [
                (100, 101, 101, 100, 101),
                (100, 102, 102, 100, 102),
                (100, 200, 200, 100, 200),
                (100, 104, 250, 99, 104),
            ]
        ),
        threshold_type="quantile",
        quantile_p=0.5,
        quantile_window=3,
    )

    assert pd.isna(result.loc[0, "trend_threshold"])
    assert pd.isna(result.loc[1, "trend_threshold"])
    assert pd.isna(result.loc[2, "trend_threshold"])
    assert result.loc[3, "trend_threshold"] == 2
    assert bool(result.loc[3, "trend_is_bar"]) is True


def test_atr_threshold_uses_shifted_rolling_true_range():
    result = add_trend_macd(
        _bars(
            [
                (10, 11, 12, 9, 11),
                (11, 12, 13, 10, 12),
                (12, 30, 100, 1, 30),
            ]
        ),
        threshold_type="atr_ratio",
        atr_k=0.5,
        atr_window=2,
    )

    assert pd.isna(result.loc[0, "trend_threshold"])
    assert pd.isna(result.loc[1, "trend_threshold"])
    assert result.loc[2, "trend_threshold"] == 1.5


def test_warmup_rows_do_not_produce_trend_bars_for_dynamic_thresholds():
    result = add_trend_macd(
        _bars(
            [
                (100, 110, 110, 100, 110),
                (110, 120, 120, 110, 120),
                (120, 130, 130, 120, 130),
            ]
        ),
        threshold_type="quantile",
        quantile_p=0.7,
        quantile_window=5,
    )

    assert not result["trend_is_bar"].any()


def test_non_trend_bars_freeze_macd_signal_and_hist():
    result = add_trend_macd(
        _bars(
            [
                (100, 110, 111, 99, 110),
                (110, 111, 112, 109, 111),
                (111, 121, 122, 110, 121),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
        fast_period=2,
        slow_period=3,
        signal_period=2,
    )

    assert bool(result.loc[1, "trend_is_bar"]) is False
    assert result.loc[1, "trend_macd"] == result.loc[0, "trend_macd"]
    assert result.loc[1, "trend_macd_signal"] == result.loc[0, "trend_macd_signal"]
    assert result.loc[1, "trend_macd_hist"] == result.loc[0, "trend_macd_hist"]


def test_cross_signals_only_fire_on_trend_bars():
    result = add_trend_macd(
        _bars(
            [
                (100, 110, 111, 99, 110),
                (110, 111, 112, 109, 111),
                (111, 125, 126, 110, 125),
            ]
        ),
        threshold_type="fixed",
        fixed_points=5,
        fast_period=2,
        slow_period=3,
        signal_period=2,
    )

    assert not result.loc[1, "trend_macd_golden_cross"]
    assert not result.loc[1, "trend_macd_death_cross"]
    assert not (
        result["trend_macd_golden_cross"] & ~result["trend_is_bar"]
    ).any()
    assert not (
        result["trend_macd_death_cross"] & ~result["trend_is_bar"]
    ).any()


def test_returns_copy_and_does_not_mutate_input():
    frame = _bars([(100, 110, 111, 99, 110)])
    original = frame.copy(deep=True)

    result = add_trend_macd(frame, threshold_type="fixed", fixed_points=5)

    pd.testing.assert_frame_equal(frame, original)
    assert "trend_macd" not in frame.columns
    assert "trend_macd" in result.columns


def test_trend_macd_runs_on_aggregated_5m_15m_and_30m_bars():
    session_start = datetime(2025, 1, 2, 9, 1)
    one_minute = pd.DataFrame(
        [
            {
                "datetime": session_start + timedelta(minutes=index),
                "open": 100 + index,
                "high": 101 + index,
                "low": 99 + index,
                "close": 100 + index,
                "volume": 1,
                "open_interest": 1000,
                "active_symbol": "RB2501",
                "flags": 0,
            }
            for index in range(30)
        ]
    )

    for minutes in [5, 15, 30]:
        bars = aggregate_continuous_1m_to_Nm(one_minute, minutes)
        result = add_trend_macd(bars, threshold_type="fixed", fixed_points=1)
        assert len(result) == len(bars)
        assert "trend_macd" in result.columns


def test_trend_macd_columns_do_not_collide_with_close_macd_columns():
    frame = _bars([(100, 110, 111, 99, 110), (110, 120, 121, 109, 120)])
    frame["close_macd"] = [0.0, 1.0]

    result = add_trend_macd(frame, threshold_type="fixed", fixed_points=5)

    assert list(result["close_macd"]) == [0.0, 1.0]
    assert "trend_macd" in result.columns


def test_invalid_parameters_raise_clear_errors():
    with pytest.raises(ValueError, match="fixed_points must be positive"):
        add_trend_macd(_bars([(100, 101, 101, 100, 101)]), threshold_type="fixed")

    with pytest.raises(ValueError, match="atr_k must be positive"):
        add_trend_macd(_bars([(100, 101, 101, 100, 101)]), threshold_type="atr_ratio", atr_window=20)

    with pytest.raises(ValueError, match="quantile_p must be between 0 and 1"):
        add_trend_macd(
            _bars([(100, 101, 101, 100, 101)]),
            threshold_type="quantile",
            quantile_p=1,
            quantile_window=20,
        )

    with pytest.raises(ValueError, match="method must be 'freeze'"):
        add_trend_macd(
            _bars([(100, 101, 101, 100, 101)]),
            threshold_type="fixed",
            fixed_points=1,
            method="skip",
        )

    with pytest.raises(ValueError, match="move_mode must be one of"):
        add_trend_macd(
            _bars([(100, 101, 101, 100, 101)]),
            threshold_type="fixed",
            fixed_points=1,
            move_mode="unknown",
        )


def _bars(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "datetime": START + timedelta(minutes=index * 5),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1,
            }
            for index, (open_, close, high, low, close) in enumerate(rows)
        ]
    )
