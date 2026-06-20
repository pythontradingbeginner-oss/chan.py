from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_foundation import (
    DirectionalChangeDetector,
    add_dc_pivots,
    detect_dc_pivots,
)
from data_foundation.pivots import DirectionalChangeDetector as DetectorFromModule


START = datetime(2026, 1, 1, 9, 0)


def test_can_import_public_pivot_api():
    assert DirectionalChangeDetector is DetectorFromModule


def test_initial_price_needs_full_threshold_to_confirm_valley():
    detector = DirectionalChangeDetector(threshold_points=10)

    assert detector.update(3560, _at_bar(0)) is None
    assert detector.update(3569, _at_bar(1)) is None

    event = detector.update(3570, _at_bar(2))

    assert event is not None
    assert event.kind == "valley"
    assert event.price == 3560
    assert event.timestamp == _at_bar(0)
    assert event.confirmation_price == 3570
    assert event.confirmation_timestamp == _at_bar(2)


def test_initial_price_needs_full_threshold_to_confirm_peak():
    detector = DirectionalChangeDetector(threshold_points=10)

    assert detector.update(3560, _at_bar(0)) is None
    assert detector.update(3551, _at_bar(1)) is None

    event = detector.update(3550, _at_bar(2))

    assert event is not None
    assert event.kind == "peak"
    assert event.price == 3560
    assert event.timestamp == _at_bar(0)
    assert event.confirmation_price == 3550
    assert event.confirmation_timestamp == _at_bar(2)


def test_candidate_peak_updates_until_threshold_reversal():
    detector = DirectionalChangeDetector(threshold_points=10)

    detector.update(3560, _at_bar(0))
    detector.update(3570, _at_bar(1))
    detector.update(3580, _at_bar(2))
    assert detector.update(3571, _at_bar(3)) is None

    event = detector.update(3570, _at_bar(4))

    assert event is not None
    assert event.kind == "peak"
    assert event.price == 3580
    assert event.timestamp == _at_bar(2)
    assert event.confirmation_price == 3570
    assert event.confirmation_timestamp == _at_bar(4)
    assert event.tmv == 2
    assert event.elapsed_minutes == 10
    assert event.r == 0.2


def test_candidate_valley_updates_until_threshold_rebound():
    detector = DirectionalChangeDetector(threshold_points=10)

    detector.update(3560, _at_bar(0))
    detector.update(3570, _at_bar(1))
    detector.update(3580, _at_bar(2))
    detector.update(3570, _at_bar(3))
    detector.update(3510, _at_bar(4))
    assert detector.update(3519, _at_bar(5)) is None

    event = detector.update(3520, _at_bar(6))

    assert event is not None
    assert event.kind == "valley"
    assert event.price == 3510
    assert event.timestamp == _at_bar(4)
    assert event.confirmation_price == 3520
    assert event.confirmation_timestamp == _at_bar(6)
    assert event.tmv == 7
    assert event.elapsed_minutes == 10
    assert event.r == 0.7


def test_unfinished_tail_swing_does_not_emit_potential_valley():
    events = detect_dc_pivots(_bars([100, 110, 130, 120, 90, 99]))

    assert list(zip(events["dc_pivot_kind"], events["dc_pivot_price"])) == [
        ("valley", 100),
        ("peak", 130),
    ]


def test_detect_dc_pivots_returns_standalone_event_table():
    events = detect_dc_pivots(_bars([100, 110, 130, 120]), threshold_points=10)

    assert list(events.columns) == [
        "dc_pivot_kind",
        "dc_pivot_price",
        "dc_pivot_timestamp",
        "dc_confirmation_price",
        "dc_confirmation_timestamp",
        "dc_tmv",
        "dc_elapsed_minutes",
        "dc_r",
        "dc_confirmation_row",
    ]
    assert len(events) == 2
    assert events.loc[0, "dc_pivot_kind"] == "valley"
    assert events.loc[0, "dc_confirmation_row"] == 1
    assert events.loc[1, "dc_pivot_kind"] == "peak"
    assert events.loc[1, "dc_confirmation_row"] == 3


def test_add_dc_pivots_writes_only_confirmation_rows_and_does_not_mutate_input():
    frame = _bars([100, 110, 130, 120])
    original = frame.copy(deep=True)

    result = add_dc_pivots(frame, threshold_points=10)

    pd.testing.assert_frame_equal(frame, original)
    assert "dc_pivot_kind" not in frame.columns
    assert pd.isna(result.loc[0, "dc_pivot_kind"])
    assert result.loc[1, "dc_pivot_kind"] == "valley"
    assert result.loc[1, "dc_pivot_timestamp"] == _at_bar(0)
    assert pd.isna(result.loc[2, "dc_pivot_kind"])
    assert result.loc[3, "dc_pivot_kind"] == "peak"
    assert result.loc[3, "dc_pivot_timestamp"] == _at_bar(2)


def test_invalid_parameters_raise_clear_errors():
    with pytest.raises(ValueError, match="threshold_points must be positive"):
        detect_dc_pivots(_bars([100]), threshold_points=0)

    with pytest.raises(ValueError, match="missing required column"):
        detect_dc_pivots(pd.DataFrame({"datetime": [_at_bar(0)]}))

    with pytest.raises(ValueError, match="missing required column"):
        add_dc_pivots(pd.DataFrame({"close": [100]}))


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
