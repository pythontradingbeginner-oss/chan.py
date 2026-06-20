from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from chan_futures.backtest import ChanBacktestConfig, ChanBacktestResult, run_chan_trigger_backtest
from data_foundation import (
    aggregate_continuous_1m_to_Nm,
    build_continuous_contract,
    clean_rb_1m_bars,
    load_raw_from_vnpy,
)
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.object import BarData

from .converter import bars_to_ohlc_frame, window_to_kl_type


APP_NAME = "ChanAnalysis"


@dataclass(frozen=True)
class ChanRunConfig:
    symbol_pattern: str = "RB%"
    symbol: str | None = None
    exchange: Exchange = Exchange.SHFE
    interval: Interval = Interval.MINUTE
    window: int = 1
    start: datetime | None = None
    end: datetime | None = None
    fee_points: float = 1.0
    slippage_points: float = 1.0
    long_only: bool = False
    limit: int | None = None


class ChanAnalysisEngine(BaseEngine):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)

    def load_bar(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        """Load exact-contract bars through vn.py's official database API."""
        database = get_database()
        return database.load_bar_data(symbol, exchange, interval, start, end)

    def run_backtest(
        self,
        bars: list[BarData] | pd.DataFrame,
        config: ChanRunConfig | None = None,
    ) -> ChanBacktestResult:
        """Run chan_futures backtest from vn.py bars or an OHLC DataFrame."""
        run_config = config or ChanRunConfig()
        kl_type = window_to_kl_type(run_config.window)
        frame = bars if isinstance(bars, pd.DataFrame) else bars_to_ohlc_frame(bars)
        return run_chan_trigger_backtest(
            frame,
            config=ChanBacktestConfig(
                kl_type=kl_type,
                fee_points=run_config.fee_points,
                slippage_points=run_config.slippage_points,
                allow_short=not run_config.long_only,
            ),
            limit=run_config.limit,
        )

    def run_from_database(self, config: ChanRunConfig | None = None) -> ChanBacktestResult:
        """Run RB continuous-main backtest or exact-contract research from vn.py data."""
        run_config = config or ChanRunConfig()
        if run_config.symbol:
            if run_config.start is None or run_config.end is None:
                raise ValueError("start and end are required for exact-contract load_bar path")
            bars = self.load_bar(
                run_config.symbol,
                run_config.exchange,
                run_config.interval,
                run_config.start,
                run_config.end,
            )
            return self.run_backtest(bars, run_config)

        if not run_config.symbol_pattern:
            raise ValueError("symbol_pattern is required for continuous-main database path")
        if run_config.interval != Interval.MINUTE:
            raise ValueError("continuous-main path expects vn.py one-minute source bars")

        raw = load_raw_from_vnpy(
            symbol_pattern=run_config.symbol_pattern,
            exchange=run_config.exchange.value,
            interval=run_config.interval.value,
            start=run_config.start,
            end=run_config.end,
        )
        cleaned, _ = clean_rb_1m_bars(raw)
        continuous, _ = build_continuous_contract(cleaned)
        frame = (
            continuous
            if run_config.window == 1
            else aggregate_continuous_1m_to_Nm(continuous, run_config.window)
        )
        return self.run_backtest(frame, run_config)

    def summarize_result(self, result: ChanBacktestResult) -> dict[str, Any]:
        if result.summary.empty:
            return {}
        return result.summary.iloc[0].to_dict()
