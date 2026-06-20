from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_foundation import plot_strategy_trade_year_charts
from data_foundation.charts import (
    plot_strategy_trade_year_charts as plot_strategy_trade_year_charts_from_charts,
)
from data_foundation.charts.strategy_trade_year import (
    _prepare_trades,
    _trade_points_for_year,
)


TARGET_STRATEGY = "final_15m_obv_sma40_filter"


def test_strategy_trade_year_chart_imports_from_top_level_and_charts_subpackage():
    assert plot_strategy_trade_year_charts is plot_strategy_trade_year_charts_from_charts


def test_plot_strategy_trade_year_charts_writes_one_png_per_year_and_filters_strategy(
    tmp_path,
):
    output_dir = tmp_path / "charts"

    result = plot_strategy_trade_year_charts(
        _bars(),
        _trades(include_unmatched_other_strategy=True),
        output_dir,
        strategy_name=TARGET_STRATEGY,
        filename_prefix="rb_strategy",
    )

    assert [path.name for path in result] == [
        "rb_strategy_2024.png",
        "rb_strategy_2025.png",
    ]
    assert all(path.exists() for path in result)
    assert all(path.stat().st_size > 0 for path in result)


def test_plot_strategy_trade_year_charts_can_disable_dc_pivots_when_columns_missing(tmp_path):
    output_dir = tmp_path / "charts_without_dc"

    result = plot_strategy_trade_year_charts(
        _bars(include_dc_pivots=False),
        _trades(),
        output_dir,
        strategy_name=TARGET_STRATEGY,
        show_dc_pivots=False,
    )

    assert len(result) == 2
    assert all(path.exists() for path in result)


def test_trade_points_for_year_splits_cross_year_entry_and_exit():
    trades = _prepare_trades(_trades(), strategy_name=TARGET_STRATEGY)

    points_2024 = _trade_points_for_year(
        trades,
        2024,
        {pd.Timestamp("2024-12-31 23:45:00"): 1},
    )
    points_2025 = _trade_points_for_year(
        trades,
        2025,
        {pd.Timestamp("2025-01-01 00:15:00"): 3},
    )

    assert list(points_2024["point"]) == ["entry"]
    assert list(points_2025["point"]) == ["exit"]
    assert points_2024.loc[0, "same_year_trade"] == False
    assert points_2025.loc[0, "same_year_trade"] == False


def test_plot_strategy_trade_year_charts_validates_required_columns(tmp_path):
    bars = _bars().drop(columns=["trend_macd"])

    with pytest.raises(ValueError, match="bars missing required column.*trend_macd"):
        plot_strategy_trade_year_charts(
            bars,
            _trades(),
            tmp_path,
            strategy_name=TARGET_STRATEGY,
        )


def test_plot_strategy_trade_year_charts_requires_dc_columns_when_enabled(tmp_path):
    bars = _bars(include_dc_pivots=False)

    with pytest.raises(ValueError, match="bars missing required column.*dc_pivot_kind"):
        plot_strategy_trade_year_charts(
            bars,
            _trades(),
            tmp_path,
            strategy_name=TARGET_STRATEGY,
        )


def test_plot_strategy_trade_year_charts_rejects_unmatched_trade_time(tmp_path):
    trades = _trades()
    trades.loc[0, "entry_time"] = "2024-12-31 23:44:00"

    with pytest.raises(ValueError, match="does not match any bar datetime"):
        plot_strategy_trade_year_charts(
            _bars(),
            trades,
            tmp_path,
            strategy_name=TARGET_STRATEGY,
        )


def test_plot_strategy_trade_year_charts_rejects_trade_price_mismatch(tmp_path):
    trades = _trades()
    trades.loc[0, "exit_close"] = 999.0

    with pytest.raises(ValueError, match="does not match bar close"):
        plot_strategy_trade_year_charts(
            _bars(),
            trades,
            tmp_path,
            strategy_name=TARGET_STRATEGY,
        )


def test_plot_strategy_trade_year_charts_accepts_structure_stop_exit_price_inside_bar(tmp_path):
    trades = _trades()
    trades.loc[0, "exit_close"] = 108.0
    trades.loc[0, "exit_reason"] = "structure_stop_exit"

    result = plot_strategy_trade_year_charts(
        _bars(),
        trades,
        tmp_path,
        strategy_name=TARGET_STRATEGY,
    )

    assert all(path.exists() for path in result)


def _bars(*, include_dc_pivots: bool = True) -> pd.DataFrame:
    start = datetime(2024, 12, 31, 23, 30)
    datetimes = [start + timedelta(minutes=15 * index) for index in range(4)]
    closes = [100.0, 105.0, 103.0, 110.0]
    data = {
        "datetime": datetimes,
        "open": [99.0, 101.0, 106.0, 104.0],
        "high": [101.0, 106.0, 107.0, 111.0],
        "low": [98.0, 100.0, 102.0, 103.0],
        "close": closes,
        "trend_macd": [-1.0, -0.5, 0.2, 0.8],
        "trend_macd_signal": [-0.8, -0.6, -0.1, 0.3],
        "trend_macd_hist": [-0.2, 0.1, 0.3, 0.5],
        "obv": [0.0, 10.0, 5.0, 18.0],
        "obv_sma40": [0.0, 5.0, 5.0, 8.25],
    }
    if include_dc_pivots:
        data["dc_pivot_kind"] = [None, "valley", None, "peak"]
        data["dc_pivot_price"] = [pd.NA, 99.0, pd.NA, 110.0]
    return pd.DataFrame(data)


def _trades(*, include_unmatched_other_strategy: bool = False) -> pd.DataFrame:
    rows = [
        {
            "strategy": TARGET_STRATEGY,
            "entry_time": "2024-12-31 23:45:00",
            "exit_time": "2025-01-01 00:15:00",
            "side": 1,
            "entry_close": 105.0,
            "exit_close": 110.0,
            "net_return_points": 5.0,
        }
    ]
    if include_unmatched_other_strategy:
        rows.append(
            {
                "strategy": "final_15m_no_obv_filter",
                "entry_time": "2030-01-01 09:00:00",
                "exit_time": "2030-01-01 09:15:00",
                "side": -1,
                "entry_close": 1.0,
                "exit_close": 2.0,
                "net_return_points": -1.0,
            }
        )
    return pd.DataFrame(rows)
