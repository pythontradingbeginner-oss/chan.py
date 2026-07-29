"""线段检测结构不变量测试。

只测结构不变量，不测精确数值输出。
"""

from __future__ import annotations

import pytest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common.CEnum import DATA_FIELD, KL_TYPE, BI_DIR
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit
from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import DATA_SRC, AUTYPE

# ── helpers ──────────────────────────────────────────────────


def _make_klu(year, month, day, hour, minute, open_, high, low, close, volume=0):
    """构造一根 CKLine_Unit，自动确保 OHLC 合法性。"""
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
    return chan[0]


def _make_trend_klus(*prices: tuple[float, float, float, float, float]):
    """从 (open, high, low, close) 元组序列生成 KLU。"""
    klus = []
    for i, (o, h, l, c) in enumerate(prices):
        t = i + 1
        klus.append(_make_klu(2025, 1, 2, 9, t, o, h, l, c))
    return klus


# ── 结构不变量测试 ──────────────────────────────────────────


class TestSegmentStructuralInvariants:
    """线段级别的结构不变量测试。"""

    def test_no_segment_without_enough_bis(self):
        """仅有 1-2 笔不应形成线段。"""
        # 一段上升趋势不足以产生线段
        klus = []
        t = 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3595, 3400, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 8, p - 8, p - 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        bis = list(ckl.bi_list)
        segs = list(ckl.seg_list)
        # 线段数必然 ≤ 笔数 - 2（每段至少需要 3 笔）
        assert len(segs) <= max(0, len(bis) - 2), (
            f"线段数 ({len(segs)}) 不应超过笔数-2 ({max(0, len(bis) - 2)})"
        )

    def test_segment_direction_matches_its_bis(self):
        """线段的方向由其组成笔的方向决定。"""
        # 构造上-下-上-下-上 五笔走势
        klus = []
        t = 1
        # 上升笔 1
        for p in range(3300, 3500, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 7, p - 7, p + 2))
            t += 1
        # 下降笔 1
        for p in range(3495, 3350, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 7, p - 7, p - 2))
            t += 1
        # 上升笔 2（更高）
        for p in range(3350, 3550, 5):
            klus.append(_make_klu(2025, 1, 2, 11, t, p, p + 7, p - 7, p + 2))
            t += 1
        # 下降笔 2
        for p in range(3545, 3400, -5):
            klus.append(_make_klu(2025, 1, 2, 12, t, p, p + 7, p - 7, p - 2))
            t += 1
        # 上升笔 3
        for p in range(3400, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 13, t, p, p + 7, p - 7, p + 2))
            t += 1

        ckl = _run_chan_on_klus(klus)
        segs = list(ckl.seg_list)
        assert len(segs) > 0, f"应至少检测到一条线段, 实际={len(segs)}"

        for seg in segs:
            assert seg.is_up() or seg.is_down(), f"线段 {seg.idx} 必须具有方向"
            if seg.is_up():
                assert seg.get_begin_val() < seg.get_end_val(), (
                    f"上升段 {seg.idx}: begin_val < end_val"
                )
            else:
                assert seg.get_begin_val() > seg.get_end_val(), (
                    f"下降段 {seg.idx}: begin_val > end_val"
                )

    def test_segment_spans_at_least_three_bis(self):
        """任何确认线段至少包含 3 笔。"""
        klus = []
        t = 1
        for p in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3545, 3300, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 11, t, p, p + 8, p - 8, p + 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for seg in ckl.seg_list:
            if seg.is_sure:
                bi_count = seg.end_bi.idx - seg.start_bi.idx + 1
                assert bi_count >= 3, (
                    f"确认段 {seg.idx}: 至少需要 3 笔, 实际={bi_count}"
                )

    def test_segment_direction_alternates(self):
        """相邻线段的方向必须交替（用不同日期的数据保持时间单调）。"""
        klus = []
        t = 1
        day = 2
        for cycle in range(3):
            for p in range(3300, 3550, 5):
                klus.append(_make_klu(2025, 1, day, 9, t, p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3545, 3300, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t, p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        ckl = _run_chan_on_klus(klus)
        segs = list(ckl.seg_list)
        if len(segs) >= 2:
            for i in range(1, len(segs)):
                assert segs[i].dir != segs[i - 1].dir, (
                    f"段 {i} 方向 ({segs[i].dir}) 应与段 {i-1} ({segs[i-1].dir}) 交替"
                )

    def test_segment_owns_its_bi_list(self):
        """线段的 bi_list 包含该段的所有笔。"""
        klus = []
        t = 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3595, 3300, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3300, 3500, 5):
            klus.append(_make_klu(2025, 1, 2, 11, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3495, 3200, -5):
            klus.append(_make_klu(2025, 1, 2, 12, t, p, p + 8, p - 8, p - 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for seg in ckl.seg_list:
            if seg.is_sure:
                bis = list(seg.bi_list)
                assert len(bis) >= 1, f"段 {seg.idx} 必须包含至少 1 笔"
                for bi in bis:
                    assert bi.parent_seg is seg, (
                        f"笔 {bi.idx} 的 parent_seg 应指向段 {seg.idx}"
                    )
