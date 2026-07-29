"""SignalExtractor 结构不变量测试 + 交易日历测试。

信号提取器：去重、状态转换、特征键映射。
交易日历：节假日检测、夜盘归属。
"""

from __future__ import annotations

import pytest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime
from Common.CEnum import DATA_FIELD, KL_TYPE, BSP_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit
from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import DATA_SRC, AUTYPE
from signal_core import SignalExtractor
from signal_core.models import SignalDirection, SignalState

# ── helpers ──────────────────────────────────────────────────


def _make_klu(year, month, day, hour, minute, open_, high, low, close, volume=0):
    total_minutes = hour * 60 + minute
    hour = total_minutes // 60
    minute = total_minutes % 60
    real_high = max(open_, high, low, close)
    real_low = min(open_, high, low, close)
    return CKLine_Unit({
        DATA_FIELD.FIELD_TIME: CTime(year, month, day, hour, minute, auto=False),
        DATA_FIELD.FIELD_OPEN: open_,
        DATA_FIELD.FIELD_HIGH: real_high,
        DATA_FIELD.FIELD_LOW: real_low,
        DATA_FIELD.FIELD_CLOSE: close,
        DATA_FIELD.FIELD_VOLUME: volume,
        DATA_FIELD.FIELD_TURNOVER: 0.0,
        DATA_FIELD.FIELD_TURNRATE: 0.0,
    })


def _make_klu_seq(klus: list[CKLine_Unit]):
    for i, klu in enumerate(klus):
        klu.set_idx(i)
        klu.kl_type = KL_TYPE.K_1M
        if i > 0:
            klu.pre = klus[i - 1]
            klus[i - 1].next = klu


def _run_chan_on_klus(klus: list[CKLine_Unit], **chan_opts):
    _make_klu_seq(klus)
    config = CChanConfig({
        "trigger_step": True,
        "bi_strict": True,
        "divergence_rate": float("inf"),
        "bsp2_follow_1": False,
        "bsp3_follow_1": False,
        "min_zs_cnt": 0,
        "bs1_peak": False,
        "macd_algo": "peak",
        "bs_type": "1,1p,2,2s,3a,3b",
        "print_warning": False,
        "seg_algo": "chan",
        **chan_opts,
    })
    chan = CChan(
        code="TEST",
        data_src=DATA_SRC.CSV,
        lv_list=[KL_TYPE.K_1M],
        config=config,
        autype=AUTYPE.NONE,
    )
    chan.trigger_load({KL_TYPE.K_1M: klus})
    return chan


def _extract_all(cklist, extractor, dt=None):
    """提取 CKLine_List 中的所有 BSP 事件。"""
    events = []
    t = dt or datetime(2025, 1, 5, 9, 0)
    for bsp in cklist.bs_point_lst.bsp_iter():
        event = extractor.extract(bsp, chan=None, bar_end_time=t, lv_idx=0)
        if event is not None:
            events.append(event)
    return events


# ── SignalExtractor 测试 ────────────────────────────────────


class TestSignalExtractor:
    """SignalExtractor 的结构不变量测试。"""

    def test_extractor_creates_events_for_bsps(self):
        """BSP 存在时，extractor 应生成事件。"""
        klus = []
        t = 1
        day = 2
        for _ in range(4):
            for p in range(3300, 3580, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3575, 3310, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        chan = _run_chan_on_klus(klus)
        extractor = SignalExtractor(symbol="TEST", timeframe="1m")
        events = _extract_all(chan[0], extractor)
        bsp_count = len(list(chan[0].bs_point_lst.bsp_iter()))
        assert len(events) <= bsp_count, (
            f"事件数 ({len(events)}) ≤ BSP 数 ({bsp_count})"
        )
        assert len(events) > 0, "应至少产生一个事件"

    def test_dedup_by_bi_idx(self):
        """同一 (bi_idx, direction, primary_bsp) 只产生一个事件。"""
        klus = []
        t = 1
        day = 2
        for _ in range(4):
            for p in range(3300, 3580, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3575, 3310, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        chan = _run_chan_on_klus(klus)
        extractor = SignalExtractor(symbol="TEST", timeframe="1m")

        # 第一次提取
        events1 = _extract_all(chan[0], extractor)
        # 第二次提取同一数据 — 应返回 None 因为状态未变
        events2 = _extract_all(chan[0], extractor)
        assert len(events2) == 0, (
            f"第二次提取同一数据应返回 0 个事件（已去重），实际={len(events2)}"
        )

    def test_event_has_required_fields(self):
        """每个 SignalEvent 必须包含必填字段。"""
        klus = []
        t = 1
        day = 2
        for _ in range(4):
            for p in range(3300, 3580, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3575, 3310, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        chan = _run_chan_on_klus(klus)
        extractor = SignalExtractor(symbol="TEST", timeframe="1m")
        events = _extract_all(chan[0], extractor)

        for event in events:
            assert event.event_id, "event_id 不应为空"
            assert event.signal_key, "signal_key 不应为空"
            assert event.direction in (
                SignalDirection.LONG, SignalDirection.SHORT
            ), f"direction 非法: {event.direction}"
            assert event.state in (
                SignalState.CANDIDATE, SignalState.CONFIRMED
            ), f"state 非法: {event.state}"
            assert event.reference_price > 0, (
                f"reference_price 必须为正数: {event.reference_price}"
            )


# ── 交易日历测试 ──────────────────────────────────────────────


class TestTradingCalendar:
    """交易日历的结构不变量测试。"""

    def test_calendar_loads_default(self):
        """默认日历可以正常加载。"""
        from data_foundation.data.calendar import RBTradingCalendar
        cal = RBTradingCalendar.load_default()
        assert cal.first_day is not None, "日历应有 first_day"
        assert cal.last_day is not None, "日历应有 last_day"
        assert cal.first_day <= cal.last_day, "first_day ≤ last_day"
        assert len(cal.trading_days) > 0, "交易日列表不应为空"

    def test_trading_days_are_unique(self):
        """交易日列表不应有重复。"""
        from data_foundation.data.calendar import RBTradingCalendar
        cal = RBTradingCalendar.load_default()
        days = cal.trading_days["trading_day"]
        assert len(days) == len(days.unique()), "交易日不应有重复"

    def test_night_session_date_before_trading_day(self):
        """夜盘日期应严格早于交易日（周五夜盘→周一交易日）。"""
        from data_foundation.data.calendar import RBTradingCalendar
        cal = RBTradingCalendar.load_default()
        night = cal.trading_days["night_session_date"]
        trade = cal.trading_days["trading_day"]
        # 夜盘日期 ≤ 交易日（或夜盘日期为 NaT 即无夜盘）
        has_night = cal.trading_days["has_night_session"]
        for i in range(len(cal.trading_days)):
            if has_night.iloc[i]:
                assert night.iloc[i] <= trade.iloc[i], (
                    f"夜盘日期 {night.iloc[i]} ≤ 交易日 {trade.iloc[i]}"
                )

    def test_calendar_covers_known_holiday(self):
        """2020 年春节（1月24-31日）不在交易日列表中。"""
        from data_foundation.data.calendar import RBTradingCalendar
        import pandas as pd
        cal = RBTradingCalendar.load_default()
        days = set(cal.trading_days["trading_day"])
        # 2020-01-24（除夕）不应在交易日中
        assert pd.Timestamp("2020-01-24") not in days, "春节 2020-01-24 不应是交易日"
        assert pd.Timestamp("2020-01-27") not in days, "春节 2020-01-27 不应是交易日"
