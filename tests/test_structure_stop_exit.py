from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_15m_sticky_exit_research import (
    _extract_trades,
    _structure_stop_snapshot,
    simulate_exit_strategy,
)
from run_dc_structure_candidate_diagnostics import _trend_state_from_confirmed_pivots


def test_dc_structure_anchor_freezes_left_valley_for_bull_segment():
    frame = _pivot_frame(
        [
            (0, "valley", 3500.0),
            (2, "peak", 3540.0),
            (4, "valley", 3505.0),
            (6, "peak", 3570.0),
        ],
        rows=8,
    )

    result = _trend_state_from_confirmed_pivots(
        frame,
        mode="sticky",
        entry_buffer_points=10.0,
        exit_buffer_points=15.0,
    )

    assert result.loc[6, "dc_trend_state"] == "bull"
    assert result.loc[6, "dc_structure_left_valley_price"] == 3500.0
    assert result.loc[6, "dc_structure_left_valley_timestamp"] == _at_bar(0)
    assert result.loc[7, "dc_structure_id"] == result.loc[6, "dc_structure_id"]
    assert result.loc[7, "dc_structure_left_valley_price"] == 3500.0


def test_dc_structure_tracks_recent_confirmed_peak_and_valley():
    frame = _pivot_frame(
        [
            (0, "valley", 3500.0),
            (2, "peak", 3540.0),
            (4, "valley", 3505.0),
            (6, "peak", 3570.0),
        ],
        rows=8,
    )

    result = _trend_state_from_confirmed_pivots(
        frame,
        mode="sticky",
        entry_buffer_points=10.0,
        exit_buffer_points=15.0,
    )

    assert pd.isna(result.loc[1, "dc_recent_peak_price"])
    assert result.loc[3, "dc_recent_peak_price"] == 3540.0
    assert result.loc[5, "dc_recent_valley_price"] == 3505.0
    assert result.loc[7, "dc_recent_peak_price"] == 3570.0
    assert result.loc[7, "dc_recent_peak_timestamp"] == _at_bar(6)


def test_structure_stop_snapshot_uses_or_mirrors_recent_peak_for_short():
    direct = _structure_stop_snapshot(
        side=-1,
        entry_price=100.0,
        source_price=105.0,
        source_timestamp=_at_bar(0),
    )
    mirrored = _structure_stop_snapshot(
        side=-1,
        entry_price=100.0,
        source_price=96.0,
        source_timestamp=_at_bar(1),
    )

    assert direct["stop_price"] == 105.0
    assert direct["adjusted"] == False
    assert mirrored["stop_price"] == 104.0
    assert mirrored["source_price"] == 96.0
    assert mirrored["adjusted"] == True


def test_structure_stop_snapshot_uses_or_mirrors_recent_valley_for_long():
    direct = _structure_stop_snapshot(
        side=1,
        entry_price=100.0,
        source_price=95.0,
        source_timestamp=_at_bar(0),
    )
    mirrored = _structure_stop_snapshot(
        side=1,
        entry_price=100.0,
        source_price=103.0,
        source_timestamp=_at_bar(1),
    )

    assert direct["stop_price"] == 95.0
    assert direct["adjusted"] == False
    assert mirrored["stop_price"] == 97.0
    assert mirrored["source_price"] == 103.0
    assert mirrored["adjusted"] == True


def test_structure_stop_exits_long_at_fixed_stop_price_from_next_bar():
    featured = _bars()
    strategy = simulate_exit_strategy(
        featured,
        long_signal=pd.Series([True, False, False]),
        short_signal=pd.Series([False, False, False]),
        strategy_name="with_stop",
        reverse_mode="reverse",
        trail_trigger_points=800.0,
        giveback_ratio=0.5,
        fee_points=1.0,
        slippage_points=0.0,
        enable_structure_stop=True,
    )
    trades = _extract_trades(featured, strategy, _config())

    assert strategy.loc[0, "event_reason"] == "long_signal"
    assert strategy.loc[2, "event_reason"] == "structure_stop_exit"
    assert strategy.loc[2, "event_price"] == 98.0
    assert trades.loc[0, "exit_close"] == 98.0
    assert trades.loc[0, "exit_reason"] == "structure_stop_exit"
    assert trades.loc[0, "entry_structure_stop_price"] == 98.0
    assert trades.loc[0, "entry_structure_stop_source_price"] == 98.0
    assert trades.loc[0, "entry_structure_stop_adjusted"] == False
    assert trades.loc[0, "net_return_points"] == -4.0


def test_structure_stop_exits_short_at_fixed_stop_price_from_next_bar():
    featured = _bars(
        closes=[100.0, 99.0, 101.0],
        highs=[101.0, 100.0, 106.0],
        lows=[99.0, 98.0, 100.0],
        short_stop=105.0,
    )
    strategy = simulate_exit_strategy(
        featured,
        long_signal=pd.Series([False, False, False]),
        short_signal=pd.Series([True, False, False]),
        strategy_name="with_stop",
        reverse_mode="reverse",
        trail_trigger_points=800.0,
        giveback_ratio=0.5,
        fee_points=1.0,
        slippage_points=0.0,
        enable_structure_stop=True,
    )

    assert strategy.loc[2, "event_reason"] == "structure_stop_exit"
    assert strategy.loc[2, "event_price"] == 105.0


def test_structure_stop_does_not_exit_on_entry_bar():
    featured = _bars(lows=[97.0, 99.0, 99.0])
    strategy = simulate_exit_strategy(
        featured,
        long_signal=pd.Series([True, False, False]),
        short_signal=pd.Series([False, False, False]),
        strategy_name="pure_macd_with_stop",
        reverse_mode="reverse",
        trail_trigger_points=None,
        giveback_ratio=None,
        fee_points=0.0,
        slippage_points=0.0,
        enable_structure_stop=True,
    )

    assert strategy.loc[0, "event_reason"] == "long_signal"
    assert strategy.loc[0, "position"] == 1.0
    assert "structure_stop_exit" not in set(strategy["event_reason"])


def test_pure_macd_reverse_signal_takes_priority_over_structure_stop():
    featured = _bars(lows=[99.0, 97.0, 96.0], highs=[101.0, 103.0, 103.0])
    strategy = simulate_exit_strategy(
        featured,
        long_signal=pd.Series([True, False, False]),
        short_signal=pd.Series([False, True, False]),
        strategy_name="pure_macd_with_stop",
        reverse_mode="reverse",
        trail_trigger_points=None,
        giveback_ratio=None,
        fee_points=0.0,
        slippage_points=0.0,
        enable_structure_stop=True,
    )

    assert strategy.loc[0, "event_reason"] == "long_signal"
    assert strategy.loc[1, "event_reason"] == "short_signal"
    assert strategy.loc[1, "position"] == -1.0
    assert "structure_stop_exit" not in set(strategy.loc[:1, "event_reason"])


def test_structure_stop_skips_entry_bar_and_signal_bar():
    featured = _bars(lows=[97.0, 97.0, 96.0], highs=[101.0, 103.0, 103.0])
    strategy = simulate_exit_strategy(
        featured,
        long_signal=pd.Series([True, False, False]),
        short_signal=pd.Series([False, True, False]),
        strategy_name="with_stop",
        reverse_mode="reverse",
        trail_trigger_points=800.0,
        giveback_ratio=0.5,
        fee_points=0.0,
        slippage_points=0.0,
        enable_structure_stop=True,
    )

    assert strategy.loc[0, "event_reason"] == "long_signal"
    assert strategy.loc[1, "event_reason"] == "short_signal"
    assert "structure_stop_exit" not in set(strategy["event_reason"])


def _bars(
    *,
    closes: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    long_stop: float = 98.0,
    short_stop: float = 104.0,
) -> pd.DataFrame:
    closes = closes or [100.0, 102.0, 103.0]
    highs = highs or [101.0, 103.0, 104.0]
    lows = lows or [99.0, 101.0, 97.0]
    return pd.DataFrame(
        {
            "datetime": [_at_bar(index) for index in range(len(closes))],
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "dc_structure_left_valley_price": [long_stop] * len(closes),
            "dc_structure_left_valley_timestamp": [_at_bar(0)] * len(closes),
            "dc_structure_left_peak_price": [short_stop] * len(closes),
            "dc_structure_left_peak_timestamp": [_at_bar(0)] * len(closes),
            "dc_recent_valley_price": [long_stop] * len(closes),
            "dc_recent_valley_timestamp": [_at_bar(0)] * len(closes),
            "dc_recent_peak_price": [short_stop] * len(closes),
            "dc_recent_peak_timestamp": [_at_bar(0)] * len(closes),
        }
    )


def _pivot_frame(events: list[tuple[int, str, float]], *, rows: int) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "datetime": [_at_bar(index) for index in range(rows)],
            "open": np.arange(rows, dtype=float),
            "high": np.arange(rows, dtype=float),
            "low": np.arange(rows, dtype=float),
            "close": np.arange(rows, dtype=float),
            "dc_pivot_kind": [pd.NA] * rows,
            "dc_pivot_price": [np.nan] * rows,
            "dc_pivot_timestamp": [pd.NaT] * rows,
        }
    )
    for row, kind, price in events:
        frame.loc[row, "dc_pivot_kind"] = kind
        frame.loc[row, "dc_pivot_price"] = price
        frame.loc[row, "dc_pivot_timestamp"] = _at_bar(row)
    return frame


def _config() -> dict[str, object]:
    return {
        "strategy": "with_stop",
        "reverse_mode": "reverse",
        "trail_trigger_points": 800.0,
        "giveback_ratio": 0.5,
    }


def _at_bar(index: int) -> datetime:
    return datetime(2024, 1, 1, 9, 0) + timedelta(minutes=15 * index)
