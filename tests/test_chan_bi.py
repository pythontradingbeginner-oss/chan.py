"""笔检测结构不变量测试。

只测结构不变量（如"三笔方向交替才成段"），不测精确数值输出。
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
    """构造一根 CKLine_Unit。自动确保 OHLC 合法性。"""
    # 归一化时间
    total_minutes = hour * 60 + minute
    hour = total_minutes // 60
    minute = total_minutes % 60
    # 确保 OHLC 合法
    real_high = max(open_, high, low, close)
    real_low = min(open_, high, low, close)
    klu = CKLine_Unit({
        DATA_FIELD.FIELD_TIME: CTime(year, month, day, hour, minute, auto=False),
        DATA_FIELD.FIELD_OPEN: open_,
        DATA_FIELD.FIELD_HIGH: real_high,
        DATA_FIELD.FIELD_LOW: real_low,
        DATA_FIELD.FIELD_CLOSE: close,
        DATA_FIELD.FIELD_VOLUME: volume,
        DATA_FIELD.FIELD_TURNOVER: 0.0,
        DATA_FIELD.FIELD_TURNRATE: 0.0,
    })
    return klu


def _make_klu_seq(klus: list[CKLine_Unit]):
    """为 KLU 序列设置 idx 和 pre/next 链。"""
    for i, klu in enumerate(klus):
        klu.set_idx(i)
        klu.kl_type = KL_TYPE.K_1M
        if i > 0:
            klu.pre = klus[i - 1]
            klus[i - 1].next = klu


def _run_chan_on_klus(klus: list[CKLine_Unit], **chan_opts):
    """将 KLU 序列喂入 CChan 并返回 CKLine_List。"""
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


class TestBiStructuralInvariants:
    """笔级别的结构不变量测试。"""

    def test_no_bi_from_flat_line(self):
        """完全平直的 K 线不应产生笔。"""
        klus = [
            _make_klu(2025, 1, 2, 9, i, 3300, 3301, 3299, 3300)
            for i in range(1, 121)
        ]
        ckl = _run_chan_on_klus(klus)
        assert len(ckl.bi_list) == 0, "完全平直的 K 线不应产生任何笔"

    def test_single_bi_requires_minimal_span(self):
        """仅有 3 根原始 K 线不足以成笔。"""
        klus = [
            _make_klu(2025, 1, 2, 9, 1, 3300, 3305, 3295, 3304),
            _make_klu(2025, 1, 2, 9, 2, 3304, 3310, 3302, 3310),
            _make_klu(2025, 1, 2, 9, 3, 3310, 3315, 3308, 3312),
        ]
        ckl = _run_chan_on_klus(klus)
        assert len(ckl.bi_list) == 0, "仅有 3 根原始 K 线不足以成笔"

    def test_bi_amplitude_positive(self):
        """笔的振幅始终为正数（需要足够多的 K 线形成笔）。"""
        klus = []
        t = 1
        # 用较大幅度的单向趋势 + 反方向趋势形成明确的笔
        for p in range(3300, 3650, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 8, p - 8, p + 3))
            t += 1
        for p in range(3645, 3295, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 8, p - 8, p - 3))
            t += 1
        for p in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, 2, 11, t, p, p + 8, p - 8, p + 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        assert len(ckl.bi_list) > 0, f"应至少检测到一笔，但只有 {len(ckl.bi_list)} 笔"
        for bi in ckl.bi_list:
            assert bi.amp() > 0, f"笔 {bi.idx} 的振幅必须为正数, 实际={bi.amp()}"

    def test_bi_begin_end_order(self):
        """笔的结束 KLC 一定在开始 KLC 之后。"""
        klus = []
        t = 1
        for price in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t,
                                  price, price + 5, price - 5, price + 3))
            t += 1
        for price in range(3550, 3301, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t,
                                  price, price + 5, price - 5, price - 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for bi in ckl.bi_list:
            assert bi.begin_klc.idx < bi.end_klc.idx, (
                f"笔 {bi.idx}: begin_klc.idx={bi.begin_klc.idx} < end_klc.idx={bi.end_klc.idx}"
            )

    def test_bi_direction_and_price_consistency(self):
        """上笔的收盘价应高于开盘价对应的方向一致性。"""
        klus = []
        t = 1
        for price in range(3300, 3550, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t,
                                  price, price + 5, price - 5, price + 3))
            t += 1
        for price in range(3550, 3301, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t,
                                  price, price + 5, price - 5, price - 3))
            t += 1

        ckl = _run_chan_on_klus(klus)
        for bi in ckl.bi_list:
            if bi.is_up():
                assert bi.get_begin_val() < bi.get_end_val(), (
                    f"上升笔 {bi.idx}: begin_val={bi.get_begin_val()} < end_val={bi.get_end_val()}"
                )
            else:
                assert bi.get_begin_val() > bi.get_end_val(), (
                    f"下降笔 {bi.idx}: begin_val={bi.get_begin_val()} > end_val={bi.get_end_val()}"
                )

    def test_strict_bi_minimal_klc_span(self):
        """严格模式：任何形成的笔 KLC 跨度必须 ≥ 4。"""
        klus = []
        t = 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 6, p - 6, p + 2))
            t += 1

        ckl = _run_chan_on_klus(klus, bi_strict=True)
        for bi in ckl.bi_list:
            span = bi.end_klc.idx - bi.begin_klc.idx + 1
            assert span >= 4, f"严格笔 {bi.idx} KLC 跨度={span}，必须 ≥ 4"

    def test_loose_bi_not_fewer_than_strict(self):
        """宽松模式产出的笔数 ≥ 严格模式。"""
        klus = []
        t = 1
        for p in range(3300, 3600, 5):
            klus.append(_make_klu(2025, 1, 2, 9, t, p, p + 6, p - 6, p + 2))
            t += 1
        for p in range(3595, 3301, -5):
            klus.append(_make_klu(2025, 1, 2, 10, t, p, p + 6, p - 6, p - 2))
            t += 1

        ckl_strict = _run_chan_on_klus(klus, bi_strict=True)
        ckl_loose = _run_chan_on_klus(klus, bi_strict=False)

        count_strict = len(list(ckl_strict.bi_list))
        count_loose = len(list(ckl_loose.bi_list))
        assert count_loose >= count_strict, (
            f"宽松模式笔数 ({count_loose}) 应 ≥ 严格模式 ({count_strict})"
        )
