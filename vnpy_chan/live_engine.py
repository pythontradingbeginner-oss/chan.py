from __future__ import annotations

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC
from chan_futures.strategy import MinimalChanTrendStrategy
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.event import EVENT_BAR
from vnpy.trader.object import BarData

from .converter import bar_to_klu, window_to_kl_type


class ChanLiveEngine(BaseEngine):
    """v2 placeholder: subscribe to vn.py bars and emit chan.py signal logs only."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, "ChanLive")
        self.window = 1
        self.kl_type = window_to_kl_type(self.window)
        self.chan: CChan | None = None
        self.strategy = MinimalChanTrendStrategy()
        self.register_event()

    def register_event(self) -> None:
        self.event_engine.register(EVENT_BAR, self._on_bar)

    def init_chan(self, code: str, window: int = 1) -> None:
        self.window = window
        self.kl_type = window_to_kl_type(window)
        self.chan = CChan(
            code=code,
            begin_time=None,
            end_time=None,
            data_src=DATA_SRC.CSV,
            lv_list=[self.kl_type],
            config=CChanConfig({"trigger_step": True, "print_warning": False}),
            autype=AUTYPE.NONE,
        )

    def _on_bar(self, event: Event) -> None:
        bar: BarData = event.data
        if getattr(bar, "interval", Interval.MINUTE) != Interval.MINUTE:
            return
        if self.chan is None:
            self.init_chan(getattr(bar, "vt_symbol", bar.symbol), self.window)

        klu = bar_to_klu(bar, self.kl_type)
        self.chan.trigger_load({self.kl_type: [klu]})
        signal = self.strategy.on_bar(
            chan=self.chan,
            current_position=0,
            price=bar.close_price,
            timestamp=bar.datetime,
            active_symbol=bar.symbol,
        )
        if signal:
            self.main_engine.write_log(
                f"ChanLive signal {signal.action} {signal.active_symbol} "
                f"price={signal.price} bsp={signal.bsp_type}",
                self.engine_name,
            )
