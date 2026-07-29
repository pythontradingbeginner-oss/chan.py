from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from Common.CEnum import KL_TYPE
from chan_futures.rb_chan_plot import (
    DEFAULT_DATA_PATH,
    build_rb_15m_chan,
    default_output_filename,
    ensure_png_filename,
    filter_rb_15m_frame,
    render_rb_chan_plot,
    resolve_output_path,
)


def test_default_filename_and_png_suffix():
    assert (
        default_output_filename("2024-01-02 09:00", "2024-01-31 15:00")
        == "RB_15m_chan_20240102_0900_20240131_1500.png"
    )
    assert ensure_png_filename("rb_view") == "rb_view.png"
    assert ensure_png_filename("rb_view.PNG") == "rb_view.PNG"


def test_filter_rejects_invalid_time_range():
    with pytest.raises(ValueError, match="start must be earlier"):
        filter_rb_15m_frame(
            _sample_bars(60),
            start="2025-01-02 10:00",
            end="2025-01-02 09:00",
        )


def test_filter_rejects_empty_range():
    with pytest.raises(ValueError, match="该时间段没有 RB 15m 数据"):
        filter_rb_15m_frame(
            _sample_bars(60),
            start="2024-01-01 09:00",
            end="2024-01-01 15:00",
        )


def test_filter_rejects_missing_ohlc_columns():
    frame = pd.DataFrame({"datetime": [pd.Timestamp("2025-01-02 09:00")], "open": [1]})

    with pytest.raises(ValueError, match="missing required columns"):
        filter_rb_15m_frame(frame)


def test_filter_rejects_too_few_bars():
    with pytest.raises(ValueError, match="筛选后 K 线数量不足"):
        filter_rb_15m_frame(_sample_bars(10), min_bars=30)


def test_resolve_output_path_requires_explicit_overwrite(tmp_path: Path):
    output = tmp_path / "chart.png"
    output.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        resolve_output_path(output_dir=tmp_path, filename="chart.png")

    assert resolve_output_path(output_dir=tmp_path, filename="chart.png", overwrite=True) == output


def test_render_synthetic_frame_generates_nonempty_png(tmp_path: Path):
    result = render_rb_chan_plot(
        frame=_sample_bars(120),
        output_dir=tmp_path,
        filename="synthetic",
        start="2025-01-02 09:00",
        end="2025-01-03 14:45",
        display_bars=60,
    )

    assert result.output_path.exists()
    assert result.output_path.stat().st_size > 0
    assert result.bar_count == 120
    assert result.plotted_bar_count == 60
    assert result.kl_type == KL_TYPE.K_15M


def test_render_supports_explicit_overwrite(tmp_path: Path):
    output = tmp_path / "overwrite.png"
    output.write_bytes(b"old")

    result = render_rb_chan_plot(
        frame=_sample_bars(80),
        output_dir=tmp_path,
        filename=output.name,
        start="2025-01-02 09:00",
        end="2025-01-03 04:45",
        display_bars=0,
        overwrite=True,
    )

    assert result.output_path == output
    assert output.stat().st_size > len(b"old")
    assert result.plotted_bar_count == result.bar_count


def test_build_chan_uses_k_15m_without_loading_stock_api(monkeypatch):
    from Chan import CChan

    def fail_if_called(self):
        raise AssertionError("stock API should not be loaded in trigger_step mode")

    monkeypatch.setattr(CChan, "GetStockAPI", fail_if_called)

    chan = build_rb_15m_chan(_sample_bars(60))

    assert chan.lv_list == [KL_TYPE.K_15M]
    assert len(chan[KL_TYPE.K_15M]) > 0


def test_render_real_rb_parquet_small_slice(tmp_path: Path):
    if not DEFAULT_DATA_PATH.exists():
        pytest.skip("real RB 15m parquet is not available")
    try:
        result = render_rb_chan_plot(
            data_path=DEFAULT_DATA_PATH,
            output_dir=tmp_path,
            filename="real_slice.png",
            start="2024-01-02 09:00",
            end="2024-01-31 15:00",
            display_bars=300,
        )
    except ImportError as exc:
        pytest.skip(f"parquet engine is not available: {exc}")

    assert result.output_path.exists()
    assert result.output_path.stat().st_size > 0
    assert result.kl_type == KL_TYPE.K_15M
    assert result.bar_count >= 300


def test_ui_smoke_core_controls(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from App.rb_chan_plot.ui import RbChanPlotWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = RbChanPlotWindow(data_path=tmp_path / "missing.parquet", output_dir=tmp_path)
    try:
        assert window.data_path_edit.text().endswith("missing.parquet")
        assert window.output_dir_edit.text() == str(tmp_path)
        assert window.filename_edit.text().endswith(".png")
        assert window.display_bars_spin.value() == 600
        assert window.generate_button.text() == "生成"
    finally:
        window.close()
        app.processEvents()


def _sample_bars(count: int) -> pd.DataFrame:
    rows = []
    price = 3300.0
    for idx in range(count):
        leg = 1 if (idx // 12) % 2 == 0 else -1
        drift = leg * (2.0 + (idx % 3) * 0.2)
        open_price = price
        close_price = price + drift
        high = max(open_price, close_price) + 2.5
        low = min(open_price, close_price) - 2.5
        rows.append(
            {
                "datetime": pd.Timestamp("2025-01-02 09:00") + pd.Timedelta(minutes=15 * idx),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": 100 + idx,
                "open_interest": 1000 + idx,
                "active_symbol": "RB2505",
                "flags": 0,
            }
        )
        price = close_price
    return pd.DataFrame(rows)
