from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_dc30_structure_score_predictive_research.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "run_dc30_structure_score_predictive_research",
    _SCRIPT_PATH,
)
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)

add_dc_structure_score = _MODULE.add_dc_structure_score
extract_trend_macd_signal_trades = _MODULE.extract_trend_macd_signal_trades
map_higher_timeframe_columns = _MODULE.map_higher_timeframe_columns


def test_dc_structure_score_uses_confirmed_same_kind_pivot_deltas():
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-01-01 09:00", periods=5, freq="30min"),
            "dc_pivot_kind": ["valley", "peak", "valley", "peak", None],
            "dc_pivot_price": [100.0, 130.0, 115.0, 145.0, None],
        }
    )

    result = add_dc_structure_score(frame, threshold_points=10)

    assert pd.isna(result.loc[0, "dc_structure_score"])
    assert pd.isna(result.loc[2, "dc_structure_score"])
    assert result.loc[3, "dc_peak_delta_points"] == 15.0
    assert result.loc[3, "dc_valley_delta_points"] == 15.0
    assert result.loc[3, "dc_structure_score"] == 1.5
    assert result.loc[4, "dc_structure_score"] == 1.5


def test_score_mapping_waits_until_higher_timeframe_bar_is_complete():
    lower = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                [
                    "2026-01-01 09:25",
                    "2026-01-01 09:30",
                    "2026-01-01 09:35",
                    "2026-01-01 10:00",
                ]
            )
        }
    )
    higher = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-01 09:00", "2026-01-01 09:30"]),
            "dc_structure_score": [1.25, -0.5],
        }
    )

    result = map_higher_timeframe_columns(
        lower,
        higher,
        higher_freq_minutes=30,
        columns=["dc_structure_score"],
        prefix="dc30",
    )

    assert pd.isna(result.loc[0, "dc30_structure_score"])
    assert result.loc[1, "dc30_structure_score"] == 1.25
    assert result.loc[2, "dc30_structure_score"] == 1.25
    assert result.loc[3, "dc30_structure_score"] == -0.5


def test_signal_trade_alignment_score_respects_trade_side():
    featured = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-01-01 09:00", periods=4, freq="5min"),
            "close": [100.0, 110.0, 105.0, 95.0],
            "trend_macd_golden_cross": [True, False, False, False],
            "trend_macd_death_cross": [False, False, True, False],
            "dc30_structure_score": [0.8, 0.8, -0.4, -0.4],
        }
    )

    trades = extract_trend_macd_signal_trades(
        featured,
        score_col="dc30_structure_score",
        fee_points=1.0,
        slippage_points=1.0,
    )

    assert len(trades) == 1
    assert trades.loc[0, "side_name"] == "long"
    assert trades.loc[0, "directional_alignment_score"] == 0.8
    assert trades.loc[0, "gross_return_points"] == 5.0
    assert trades.loc[0, "cost_points"] == 6.0
    assert trades.loc[0, "net_return_points"] == -1.0
