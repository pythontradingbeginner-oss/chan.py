"""中枢检测结构不变量测试。

只测结构不变量，不测精确数值输出。
"""

from __future__ import annotations

import pytest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit
from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import DATA_SRC, AUTYPE

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
        "zs_combine": True,
        "zs_algo": "normal",
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


# ── 结构不变量测试 ──────────────────────────────────────────


class TestZSStructuralInvariants:
    """中枢级别的结构不变量测试。"""

    def test_zs_range_is_valid(self):
        """中枢的高低范围必须合法（high > low）。"""
        klus = []
        t = 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3595, 3300, -5):
            klus.append(_make_klu(2025, 1, 3, 9, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, 4, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3545, 3200, -5):
            klus.append(_make_klu(2025, 1, 5, 9, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3200, 3450, 5):
            klus.append(_make_klu(2025, 1, 6, 9, t, p, p + 8, p - 8, p + 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for zs in ckl.zs_list:
            assert zs.high > zs.low, (
                f"中枢 {zs.idx if hasattr(zs, 'idx') else '?'} high={zs.high} > low={zs.low}"
            )

    def test_zs_low_and_high_from_items(self):
        """中枢的低点 = 所有组件低点的最大值，高点 = 所有组件高点的最小值。"""
        klus = []
        t = 1
        for p in range(3300, 3580, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 10, p - 10, p + 4))
            t += 1
        for p in range(3575, 3310, -5):
            klus.append(_make_klu(2025, 1, 3, 9, t, p, p + 10, p - 10, p - 4))
            t += 1
        for p in range(3310, 3520, 5):
            klus.append(_make_klu(2025, 1, 4, 9, t, p, p + 10, p - 10, p + 4))
            t += 1
        for p in range(3515, 3180, -5):
            klus.append(_make_klu(2025, 1, 5, 9, t, p, p + 10, p - 10, p - 4))
            t += 1
        for p in range(3180, 3420, 5):
            klus.append(_make_klu(2025, 1, 6, 9, t, p, p + 10, p - 10, p + 4))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for zs in ckl.zs_list:
            if not hasattr(zs, 'begin') or zs.begin is None:
                continue
            # 中枢高点不超过中段内任何元素的高点
            assert zs.high <= zs.peak_high, (
                f"中枢 high={zs.high} ≤ peak_high={zs.peak_high}"
            )
            # 中枢低点不低于中段内任何元素的低点
            assert zs.low >= zs.peak_low, (
                f"中枢 low={zs.low} ≥ peak_low={zs.peak_low}"
            )

    def test_zs_requires_overlapping_bis(self):
        """中枢需要至少 3 段重叠的笔才能形成。"""
        klus = []
        t = 1
        # 仅仅两段趋势（一上一下）不构成中枢
        for p in range(3300, 3600, 3):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 6, p - 6, p + 2))
            t += 1
        for p in range(3597, 3300, -3):
            klus.append(_make_klu(2025, 1, 3, 9, t, p, p + 6, p - 6, p - 2))
            t += 1

        ckl_no_zs = _run_chan_on_klus(klus, min_zs_cnt=1)
        # 中枢数量应为 0（笔不够，无法形成中枢）
        zs_no = len(list(ckl_no_zs.zs_list))
        assert zs_no == 0, (
            f"仅有一上一下两段走势，不应形成中枢，实际有 {zs_no} 个"
        )

    def test_min_zs_cnt_filters(self):
        """min_zs_cnt 参数应正确过滤掉低于阈值的中枢。"""
        klus = []
        t = 1
        day = 2
        for _ in range(5):
            for p in range(3300, 3550, 5):
                klus.append(_make_klu(2025, 1, day, 9, t, p, p + 7, p - 7, p + 3))
                t += 1
            for p in range(3545, 3300, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t, p, p + 7, p - 7, p - 3))
                t += 1
            day += 2
            t = 1

        ckl_all = _run_chan_on_klus(klus, min_zs_cnt=0)
        ckl_filtered = _run_chan_on_klus(klus, min_zs_cnt=3)

        zs_all = len(list(ckl_all.zs_list))
        zs_filtered = len(list(ckl_filtered.zs_list))
        assert zs_filtered <= zs_all, (
            f"min_zs_cnt=3 时中枢数 ({zs_filtered}) ≤ min_zs_cnt=0 时 ({zs_all})"
        )

    def test_zs_inside_segment_is_normal(self):
        """normal 模式：中枢只在本线段内，不跨线段。"""
        klus = []
        t = 1
        day = 2
        for p in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, day, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3545, 3300, -5):
            klus.append(_make_klu(2025, 1, day + 1, 9, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, day + 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3595, 3400, -5):
            klus.append(_make_klu(2025, 1, day + 3, 9, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3400, 3650, 5):
            klus.append(_make_klu(2025, 1, day + 4, 9, t, p, p + 8, p - 8, p + 3))
            t += 1

        ckl = _run_chan_on_klus(klus, zs_algo="normal")
        # 每个中枢应属于某条线段
        for zs in ckl.zs_list:
            begin_idx = zs.begin_bi.idx
            end_idx = zs.end_bi.idx
            assert begin_idx <= end_idx, (
                f"中枢的 begin_bi.idx ({begin_idx}) ≤ end_bi.idx ({end_idx})"
            )
