from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_trend_macd_5m_30m_dc_research.py"
)
_SPEC = importlib.util.spec_from_file_location("run_trend_macd_5m_30m_dc_research", _SCRIPT_PATH)
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)

map_higher_timeframe_dc_state = _MODULE.map_higher_timeframe_dc_state
sticky_weighted_bias_position = _MODULE.sticky_weighted_bias_position


def test_higher_timeframe_mapping_waits_until_bar_is_complete():
    lower = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                [
                    "2026-01-01 09:25",
                    "2026-01-01 09:30",
                    "2026-01-01 09:35",
                    "2026-01-01 10:00",
                ]
            ),
            "close": [100, 101, 102, 103],
        }
    )
    higher = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-01 09:00", "2026-01-01 09:30"]),
            "dc_trend_state": ["bull", "bear"],
            "dc_is_bull": [True, False],
            "dc_is_bear": [False, True],
        }
    )

    result = map_higher_timeframe_dc_state(
        lower,
        higher,
        higher_freq_minutes=30,
        prefix="dc30",
    )

    assert list(result["dc30_trend_state"]) == ["unknown", "bull", "bull", "bear"]
    assert list(result["dc30_is_bull"]) == [False, True, True, False]
    assert list(result["dc30_is_bear"]) == [False, False, False, True]
    assert pd.isna(result.loc[0, "dc30_available_at"])
    assert result.loc[1, "dc30_available_at"] == pd.Timestamp("2026-01-01 09:30")
    assert result.loc[3, "dc30_available_at"] == pd.Timestamp("2026-01-01 10:00")


def test_sticky_weighted_bias_sizes_held_macd_direction_by_current_state():
    featured = pd.DataFrame(
        {
            "trend_macd_golden_cross": [False, True, False, False, False],
            "trend_macd_death_cross": [False, False, False, True, False],
            "sticky_dc30_trend_state": ["unknown", "bull", "bear", "sideways", "bull"],
        }
    )

    position = sticky_weighted_bias_position(featured)

    assert list(position) == [0.0, 1.0, 0.3, -0.6, -0.3]
