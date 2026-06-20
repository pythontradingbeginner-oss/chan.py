from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from data_foundation import load_replay_bars_jsonl, plot_dc_peak_valley_chart
from data_foundation.charts import (
    load_replay_bars_jsonl as load_replay_bars_jsonl_from_charts,
)
from data_foundation.charts import (
    plot_dc_peak_valley_chart as plot_dc_peak_valley_chart_from_charts,
)


START = datetime(2026, 1, 1, 9, 0)


def test_chart_functions_can_import_from_top_level_and_charts_subpackage():
    assert load_replay_bars_jsonl is load_replay_bars_jsonl_from_charts
    assert plot_dc_peak_valley_chart is plot_dc_peak_valley_chart_from_charts


def test_load_replay_bars_jsonl_maps_replay_fields(tmp_path):
    path = tmp_path / "rb_5m_bars.jsonl"
    rows = [
        {
            "observer_available_at": "2026-01-01T09:05:00+08:00",
            "open": 100,
            "high": 103,
            "low": 99,
            "close": 102,
            "volume": 10,
            "contract_id": "RB2601",
            "trading_day": "2026-01-01",
        }
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    result = load_replay_bars_jsonl(path)

    assert list(result.columns) == [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "contract_id",
        "trading_day",
    ]
    assert result.loc[0, "close"] == 102
    assert result.loc[0, "contract_id"] == "RB2601"


def test_plot_dc_peak_valley_chart_writes_png_with_default_controls(tmp_path):
    output = tmp_path / "chart.png"

    result = plot_dc_peak_valley_chart(
        _bars([100, 110, 130, 120, 90, 105, 140, 125, 150, 130, 160, 145]),
        output,
        start="2026-01-01 09:00",
        end="2026-01-01 10:00",
        dc_threshold_points=10,
        trend_buffer_points=5,
        obv_ma_period=3,
    )

    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_plot_dc_peak_valley_chart_can_aggregate_5m_to_10m(tmp_path):
    output = tmp_path / "chart_10m.png"

    plot_dc_peak_valley_chart(
        _bars([100, 105, 110, 108, 130, 125, 90, 95, 115, 118, 140, 135]),
        output,
        kline_minutes=10,
        source_minutes=5,
        dc_threshold_points=5,
        trend_buffer_points=5,
        obv_ma_period=2,
    )

    assert output.exists()
    assert output.stat().st_size > 0


def test_plot_dc_peak_valley_chart_validates_controls(tmp_path):
    frame = _bars([100, 101])
    output = tmp_path / "chart.png"

    with pytest.raises(ValueError, match="kline_minutes must be positive"):
        plot_dc_peak_valley_chart(frame, output, kline_minutes=0)

    with pytest.raises(ValueError, match="source_minutes must be positive"):
        plot_dc_peak_valley_chart(frame, output, source_minutes=0)

    with pytest.raises(ValueError, match="kline_minutes must be a multiple"):
        plot_dc_peak_valley_chart(frame, output, kline_minutes=7, source_minutes=5)

    with pytest.raises(ValueError, match="obv_ma_period must be positive"):
        plot_dc_peak_valley_chart(frame, output, obv_ma_period=0)

    with pytest.raises(ValueError, match="min_width must be positive"):
        plot_dc_peak_valley_chart(frame, output, min_width=0)

    with pytest.raises(ValueError, match="width_per_bar must be positive"):
        plot_dc_peak_valley_chart(frame, output, width_per_bar=0)

    with pytest.raises(ValueError, match="max_width must be positive"):
        plot_dc_peak_valley_chart(frame, output, max_width=0)

    with pytest.raises(ValueError, match="max_width must be greater than or equal to min_width"):
        plot_dc_peak_valley_chart(frame, output, min_width=20, max_width=10)

    with pytest.raises(ValueError, match="missing required column"):
        plot_dc_peak_valley_chart(pd.DataFrame({"datetime": [_at_bar(0)]}), output)

    with pytest.raises(ValueError, match="no bars available"):
        plot_dc_peak_valley_chart(
            frame,
            output,
            start="2026-01-02 09:00",
            end="2026-01-02 10:00",
        )


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [_at_bar(index) for index in range(len(closes))],
            "open": closes,
            "high": [price + 1 for price in closes],
            "low": [price - 1 for price in closes],
            "close": closes,
            "volume": [index + 1 for index in range(len(closes))],
        }
    )


def _at_bar(index: int) -> datetime:
    return START + timedelta(minutes=index * 5)
