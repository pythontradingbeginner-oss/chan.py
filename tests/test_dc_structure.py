from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_foundation import (
    DirectionalChangeDetector,
    add_dc_macd_comparison,
    add_dc_pivots,
    add_dc_structure,
    detect_dc_pivots,
)
from data_foundation.features import (
    add_dc_macd_comparison as add_dc_macd_comparison_from_features,
)
from data_foundation.features import add_dc_structure as add_dc_structure_from_features
from data_foundation.pivots import (
    DirectionalChangeDetector as detector_from_pivots,
)
from data_foundation.pivots import add_dc_pivots as add_dc_pivots_from_pivots
from data_foundation.pivots import detect_dc_pivots as detect_dc_pivots_from_pivots


START = datetime(2026, 1, 1, 9, 0)


def test_can_import_from_top_level_and_features_subpackage():
    assert add_dc_structure is add_dc_structure_from_features
    assert add_dc_macd_comparison is add_dc_macd_comparison_from_features
    assert DirectionalChangeDetector is detector_from_pivots
    assert add_dc_pivots is add_dc_pivots_from_pivots
    assert detect_dc_pivots is detect_dc_pivots_from_pivots


def test_initial_price_needs_full_threshold_to_confirm_valley():
    result = add_dc_structure(_bars([3560, 3569, 3570]), threshold_points=10)

    assert pd.isna(result.loc[1, "dc_pivot_kind"])
    assert result.loc[2, "dc_pivot_kind"] == "valley"
    assert result.loc[2, "dc_pivot_price"] == 3560
    assert result.loc[2, "dc_pivot_timestamp"] == _at_bar(0)
    assert result.loc[2, "dc_confirmation_price"] == 3570
    assert result.loc[2, "dc_confirmation_timestamp"] == _at_bar(2)


def test_initial_price_needs_full_threshold_to_confirm_peak():
    result = add_dc_structure(_bars([3560, 3551, 3550]), threshold_points=10)

    assert result.loc[2, "dc_pivot_kind"] == "peak"
    assert result.loc[2, "dc_pivot_price"] == 3560
    assert result.loc[2, "dc_pivot_timestamp"] == _at_bar(0)
    assert result.loc[2, "dc_confirmation_price"] == 3550
    assert result.loc[2, "dc_confirmation_timestamp"] == _at_bar(2)


def test_candidate_peak_updates_until_threshold_reversal():
    result = add_dc_structure(_bars([3560, 3570, 3580, 3571, 3570]))

    event = result.loc[4]
    assert event["dc_pivot_kind"] == "peak"
    assert event["dc_pivot_price"] == 3580
    assert event["dc_pivot_timestamp"] == _at_bar(2)
    assert event["dc_confirmation_price"] == 3570
    assert event["dc_confirmation_timestamp"] == _at_bar(4)
    assert event["dc_tmv"] == 2
    assert event["dc_elapsed_minutes"] == 10
    assert event["dc_r"] == 0.2


def test_candidate_valley_updates_until_threshold_rebound():
    result = add_dc_structure(_bars([3560, 3570, 3580, 3570, 3510, 3519, 3520]))

    event = result.loc[6]
    assert event["dc_pivot_kind"] == "valley"
    assert event["dc_pivot_price"] == 3510
    assert event["dc_pivot_timestamp"] == _at_bar(4)
    assert event["dc_confirmation_price"] == 3520
    assert event["dc_confirmation_timestamp"] == _at_bar(6)
    assert event["dc_tmv"] == 7
    assert event["dc_elapsed_minutes"] == 10
    assert event["dc_r"] == 0.7


def test_unfinished_tail_swing_does_not_emit_potential_valley():
    result = add_dc_structure(_bars([100, 110, 130, 120, 90, 99]))

    events = result[result["dc_pivot_kind"].notna()]

    assert list(zip(events["dc_pivot_kind"], events["dc_pivot_price"])) == [
        ("valley", 100),
        ("peak", 130),
    ]


def test_trend_remains_unknown_until_two_peaks_and_two_valleys():
    result = add_dc_structure(_bars([3500, 3510, 3540, 3530, 3520, 3530, 3535]))

    assert result["dc_trend_state"].iloc[-1] == "unknown"
    assert not result["dc_is_bull"].any()
    assert not result["dc_is_bear"].any()


def test_trend_classifies_bull_and_state_persists_after_confirmation():
    result = add_dc_structure(_bars([3500, 3510, 3540, 3530, 3520, 3530, 3570, 3560, 3565]))

    assert result.loc[7, "dc_pivot_kind"] == "peak"
    assert result.loc[7, "dc_trend_state"] == "bull"
    assert result.loc[7, "dc_is_bull"] == True
    assert result.loc[8, "dc_trend_state"] == "bull"
    assert result.loc[8, "dc_is_bull"] == True


def test_trend_classifies_bear():
    result = add_dc_structure(_bars([3570, 3560, 3530, 3540, 3550, 3540, 3500, 3510]))

    assert result.loc[7, "dc_trend_state"] == "bear"
    assert result.loc[7, "dc_is_bear"] == True


def test_trend_classifies_sideways_when_structure_is_mixed():
    result = add_dc_structure(_bars([3530, 3540, 3550, 3540, 3510, 3520, 3570, 3560]))

    assert result.loc[7, "dc_trend_state"] == "sideways"
    assert result.loc[7, "dc_is_bull"] == False
    assert result.loc[7, "dc_is_bear"] == False


def test_trend_buffer_filters_small_structure_changes():
    result = add_dc_structure(_bars([3500, 3510, 3540, 3530, 3509, 3519, 3549, 3539]))

    assert result.loc[7, "dc_trend_state"] == "sideways"


def test_sticky_trend_enters_bull_on_single_confirming_leg():
    result = add_dc_structure(
        _bars([3500, 3510, 3540, 3530, 3505, 3515, 3570, 3560]),
        trend_mode="sticky",
        trend_entry_buffer_points=10,
        trend_exit_buffer_points=15,
    )

    assert result.loc[7, "dc_trend_state"] == "bull"
    assert result.loc[7, "dc_is_bull"] == True


def test_sticky_trend_enters_bear_on_single_confirming_leg():
    result = add_dc_structure(
        _bars([3570, 3560, 3530, 3540, 3565, 3555, 3500, 3510]),
        trend_mode="sticky",
        trend_entry_buffer_points=10,
        trend_exit_buffer_points=15,
    )

    assert result.loc[7, "dc_trend_state"] == "bear"
    assert result.loc[7, "dc_is_bear"] == True


def test_sticky_trend_stays_sideways_when_structure_conflicts():
    result = add_dc_structure(
        _bars([3500, 3510, 3540, 3530, 3480, 3490, 3570, 3560]),
        trend_mode="sticky",
        trend_entry_buffer_points=10,
        trend_exit_buffer_points=15,
    )

    assert result.loc[7, "dc_trend_state"] == "sideways"
    assert result.loc[7, "dc_is_bull"] == False
    assert result.loc[7, "dc_is_bear"] == False


def test_sticky_bull_requires_both_legs_to_break_before_exit():
    result = add_dc_structure(
        _bars([3500, 3510, 3540, 3530, 3505, 3515, 3570, 3560, 3480, 3490, 3530, 3520]),
        trend_mode="sticky",
        trend_entry_buffer_points=10,
        trend_exit_buffer_points=15,
    )

    assert result.loc[7, "dc_trend_state"] == "bull"
    assert result.loc[9, "dc_trend_state"] == "bull"
    assert result.loc[11, "dc_trend_state"] == "sideways"
    assert result.loc[11, "dc_is_bull"] == False
    assert result.loc[11, "dc_is_bear"] == False


def test_sticky_bear_requires_both_legs_to_break_before_exit():
    result = add_dc_structure(
        _bars([3570, 3560, 3530, 3540, 3565, 3555, 3500, 3510, 3585, 3575, 3550, 3560]),
        trend_mode="sticky",
        trend_entry_buffer_points=10,
        trend_exit_buffer_points=15,
    )

    assert result.loc[7, "dc_trend_state"] == "bear"
    assert result.loc[9, "dc_trend_state"] == "bear"
    assert result.loc[11, "dc_trend_state"] == "sideways"
    assert result.loc[11, "dc_is_bull"] == False
    assert result.loc[11, "dc_is_bear"] == False


def test_returns_copy_with_same_row_count_and_does_not_mutate_input():
    frame = _bars([100, 110, 130, 120])
    original = frame.copy(deep=True)

    result = add_dc_structure(frame)

    assert len(result) == len(frame)
    pd.testing.assert_frame_equal(frame, original)
    assert "dc_trend_state" not in frame.columns
    assert "dc_trend_state" in result.columns


def test_recent_confirmed_peak_and_valley_are_forward_filled_after_confirmation():
    frame = _bars([3560, 3570, 3580, 3570, 3510, 3519, 3520])
    original = frame.copy(deep=True)

    result = add_dc_structure(frame, threshold_points=10)

    pd.testing.assert_frame_equal(frame, original)
    assert "dc_recent_peak_price" not in frame.columns
    assert pd.isna(result.loc[0, "dc_recent_valley_price"])
    assert result.loc[1, "dc_recent_valley_price"] == 3560
    assert result.loc[1, "dc_recent_valley_timestamp"] == _at_bar(0)
    assert result.loc[2, "dc_recent_valley_price"] == 3560
    assert result.loc[3, "dc_recent_peak_price"] == 3580
    assert result.loc[3, "dc_recent_peak_timestamp"] == _at_bar(2)
    assert result.loc[4, "dc_recent_peak_price"] == 3580
    assert result.loc[6, "dc_recent_valley_price"] == 3510
    assert result.loc[6, "dc_recent_valley_timestamp"] == _at_bar(4)


def test_invalid_parameters_raise_clear_errors():
    with pytest.raises(ValueError, match="threshold_points must be positive"):
        add_dc_structure(_bars([100]), threshold_points=0)

    with pytest.raises(ValueError, match="trend_buffer_points must not be negative"):
        add_dc_structure(_bars([100]), trend_buffer_points=-1)

    with pytest.raises(ValueError, match="trend_mode must be one of"):
        add_dc_structure(_bars([100]), trend_mode="fast")

    with pytest.raises(ValueError, match="trend_entry_buffer_points must not be negative"):
        add_dc_structure(
            _bars([100]),
            trend_mode="sticky",
            trend_entry_buffer_points=-1,
        )

    with pytest.raises(ValueError, match="trend_exit_buffer_points must not be negative"):
        add_dc_structure(
            _bars([100]),
            trend_mode="sticky",
            trend_exit_buffer_points=-1,
        )

    with pytest.raises(ValueError, match="greater than or equal"):
        add_dc_structure(
            _bars([100]),
            trend_mode="sticky",
            trend_entry_buffer_points=10,
            trend_exit_buffer_points=5,
        )

    with pytest.raises(ValueError, match="missing required column"):
        add_dc_structure(pd.DataFrame({"datetime": [_at_bar(0)]}))

    with pytest.raises(ValueError, match="missing required column"):
        add_dc_structure(pd.DataFrame({"close": [100]}))


def test_macd_comparison_adds_close_peak_and_valley_macd_columns():
    result = add_dc_macd_comparison(
        _bars([3500, 3510, 3540, 3530, 3520, 3530, 3570, 3560, 3565]),
        fast_period=2,
        slow_period=3,
        signal_period=2,
    )

    assert result.loc[7, "dc_trend_state"] == "bull"
    assert result.loc[7, "dc_peak_price_line"] == 3570
    assert result.loc[7, "dc_valley_price_line"] == 3520
    assert result.loc[7, "dc_macd_structure"] == "structure_bull"
    assert result.loc[7, "dc_macd_price_alignment"] == "bull_confirmed"
    assert result.loc[7, "dc_bull_close_macd_death_cross"] == True
    assert result.loc[7, "dc_bull_dc_peak_macd_golden_cross"] == True
    assert result.loc[7, "dc_bull_dc_valley_macd_death_cross"] == True


def test_macd_comparison_marks_bear_crosses_inside_dc_bear_structure():
    result = add_dc_macd_comparison(
        _bars([3570, 3560, 3530, 3540, 3550, 3540, 3500, 3510, 3505, 3490]),
        fast_period=2,
        slow_period=3,
        signal_period=2,
    )

    assert result.loc[7, "dc_trend_state"] == "bear"
    assert result.loc[7, "dc_macd_structure"] == "structure_bear"
    assert result.loc[7, "dc_macd_price_alignment"] == "bear_confirmed"
    assert result.loc[7, "dc_bear_close_macd_golden_cross"] == True
    assert result.loc[7, "dc_bear_dc_peak_macd_golden_cross"] == True
    assert result.loc[7, "dc_bear_dc_valley_macd_death_cross"] == True
    assert result.loc[9, "dc_bear_close_macd_death_cross"] == True
    assert result.loc[9, "dc_bear_dc_peak_macd_death_cross"] == True
    assert result.loc[9, "dc_bear_dc_valley_macd_golden_cross"] == True


def test_macd_comparison_validates_periods():
    with pytest.raises(ValueError, match="fast_period must be positive"):
        add_dc_macd_comparison(_bars([100]), fast_period=0)

    with pytest.raises(ValueError, match="slow_period must be positive"):
        add_dc_macd_comparison(_bars([100]), slow_period=0)

    with pytest.raises(ValueError, match="signal_period must be positive"):
        add_dc_macd_comparison(_bars([100]), signal_period=0)

    with pytest.raises(ValueError, match="fast_period must be smaller than slow_period"):
        add_dc_macd_comparison(_bars([100]), fast_period=3, slow_period=3)


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [_at_bar(index) for index in range(len(closes))],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1] * len(closes),
        }
    )


def _at_bar(index: int) -> datetime:
    return START + timedelta(minutes=index * 5)
