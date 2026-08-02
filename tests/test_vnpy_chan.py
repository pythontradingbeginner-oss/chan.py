from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd
import pytest

from Common.CEnum import DATA_FIELD, KL_TYPE
from vnpy_chan.converter import bar_to_klu, bars_to_ohlc_frame, window_to_kl_type


class _Exchange(Enum):
    SHFE = "SHFE"


@dataclass
class _Bar:
    symbol: str = "RB2505"
    exchange: _Exchange = _Exchange.SHFE
    datetime: datetime = datetime(2025, 1, 2, 21, 1)
    interval: object | None = None
    volume: float = 10
    turnover: float = 33050
    open_interest: float = 1000
    open_price: float = 3300
    high_price: float = 3310
    low_price: float = 3290
    close_price: float = 3305
    gateway_name: str = "test"


def test_bar_to_klu_maps_vnpy_bar_fields():
    klu = bar_to_klu(_Bar(), KL_TYPE.K_15M)

    assert klu.kl_type == KL_TYPE.K_15M
    assert klu.time.to_str() == "2025/01/02 21:01"
    assert klu.open == 3300
    assert klu.high == 3310
    assert klu.low == 3290
    assert klu.close == 3305
    assert klu.trade_info.metric[DATA_FIELD.FIELD_VOLUME] == 10
    assert klu.trade_info.metric[DATA_FIELD.FIELD_TURNOVER] == 33050


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (1, KL_TYPE.K_1M),
        (5, KL_TYPE.K_5M),
        (15, KL_TYPE.K_15M),
        (30, KL_TYPE.K_30M),
        (60, KL_TYPE.K_60M),
    ],
)
def test_window_to_kl_type_maps_supported_windows(window, expected):
    assert window_to_kl_type(window) == expected


def test_window_to_kl_type_rejects_unsupported_window():
    with pytest.raises(ValueError, match="unsupported chan.py window"):
        window_to_kl_type(7)


def test_bars_to_ohlc_frame_uses_vnpy_bar_field_names():
    frame = bars_to_ohlc_frame([_Bar()])

    assert list(frame.columns) == [
        "symbol",
        "exchange",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "open_interest",
        "active_symbol",
        "flags",
    ]
    assert frame.iloc[0]["open"] == 3300
    assert frame.iloc[0]["close"] == 3305


def test_chan_analysis_app_metadata():
    pytest.importorskip("vnpy")
    from vnpy_chan import ChanAnalysisApp

    assert ChanAnalysisApp.app_name == "ChanAnalysis"
    assert ChanAnalysisApp.app_module == "vnpy_chan"
    assert ChanAnalysisApp.widget_name == "ChanAnalysisWidget"


def test_chan_analysis_app_registers_with_main_engine():
    pytest.importorskip("vnpy")
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_chan import ChanAnalysisApp

    cwd = Path.cwd()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    try:
        engine = main_engine.add_app(ChanAnalysisApp)

        assert engine.engine_name == "ChanAnalysis"
        assert "ChanAnalysis" in main_engine.apps
        assert main_engine.get_engine("ChanAnalysis") is engine
    finally:
        main_engine.close()
        import os

        os.chdir(cwd)


def test_chan_analysis_engine_load_bar_uses_official_database(monkeypatch):
    pytest.importorskip("vnpy")
    from vnpy.trader.constant import Exchange, Interval
    from vnpy_chan import engine as engine_module
    from vnpy_chan.engine import ChanAnalysisEngine

    calls = {}

    class _Database:
        def load_bar_data(self, symbol, exchange, interval, start, end):
            calls["args"] = (symbol, exchange, interval, start, end)
            return [_Bar(symbol=symbol)]

    monkeypatch.setattr(engine_module, "get_database", lambda: _Database())
    instance = ChanAnalysisEngine.__new__(ChanAnalysisEngine)
    bars = instance.load_bar(
        "RB2505",
        Exchange.SHFE,
        Interval.MINUTE,
        datetime(2025, 1, 1),
        datetime(2025, 1, 2),
    )

    assert bars[0].symbol == "RB2505"
    assert calls["args"][0] == "RB2505"


def test_chan_analysis_engine_run_from_database_continuous_pipeline(monkeypatch):
    pytest.importorskip("vnpy")
    from vnpy.trader.constant import Exchange
    from vnpy_chan import engine as engine_module
    from vnpy_chan.engine import ChanAnalysisEngine, ChanRunConfig

    raw = pd.DataFrame({"symbol": ["RB2505"]})
    cleaned = pd.DataFrame({"symbol": ["RB2505"]})
    continuous = pd.DataFrame(
        {
            "datetime": pd.date_range("2025-01-02 09:00", periods=3, freq="min"),
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [100.5, 101.5, 102.5],
            "volume": [1, 2, 3],
            "open_interest": [10, 11, 12],
            "active_symbol": ["RB2505"] * 3,
            "flags": [0, 0, 0],
        }
    )
    observed = {}

    monkeypatch.setattr(engine_module, "load_raw_from_vnpy", lambda **kwargs: raw)
    monkeypatch.setattr(engine_module, "clean_rb_1m_bars", lambda frame: (cleaned, object()))
    monkeypatch.setattr(engine_module, "build_continuous_contract", lambda frame: (continuous, object()))

    def fake_run(self, bars, config):
        observed["bars"] = bars
        observed["config"] = config
        return "result"

    monkeypatch.setattr(ChanAnalysisEngine, "run_backtest", fake_run)
    instance = ChanAnalysisEngine.__new__(ChanAnalysisEngine)
    result = instance.run_from_database(ChanRunConfig(exchange=Exchange.SHFE, limit=3))

    assert result == "result"
    assert observed["bars"].equals(continuous)
    assert observed["config"].limit == 3
