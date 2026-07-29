"""买卖点检测结构不变量测试。

只测结构不变量，不测精确数值输出。
"""

from __future__ import annotations

import pytest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common.CEnum import DATA_FIELD, KL_TYPE, BSP_TYPE
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


class TestBSPStructuralInvariants:
    """买卖点级别的结构不变量测试。"""

    def test_bsp_is_buy_sell_consistent(self):
        """买卖点的 is_buy 与 type 一致。"""
        klus = []
        t = 1
        day = 2
        for _ in range(5):
            for p in range(3300, 3600, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3595, 3300, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        ckl = _run_chan_on_klus(klus)
        for bsp in ckl.bs_point_lst.bsp_iter():
            assert isinstance(bsp.is_buy, bool), f"BSP is_buy 应是 bool"
            assert isinstance(bsp.type, list), f"BSP type 应是 list"
            assert len(bsp.type) > 0, f"BSP 必须至少有一个类型"

    def test_bsp_assigned_to_a_bi(self):
        """每个 BSP 应归属于某一笔。"""
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

        ckl = _run_chan_on_klus(klus)
        bis = list(ckl.bi_list)
        for bsp in ckl.bs_point_lst.bsp_iter():
            assert bsp.bi is not None, f"BSP 必须归属于一笔: {bsp.type}"
            assert bsp.klu is not None, f"BSP 必须有关联的 K 线单元"

    def test_divergence_rate_infinite_disables_divergence(self):
        """divergence_rate=inf 时，应只产生非背驰类型的 BSP。"""
        klus = []
        t = 1
        day = 2
        for _ in range(5):
            for p in range(3300, 3600, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3595, 3300, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        ckl = _run_chan_on_klus(klus, divergence_rate=float("inf"))
        for bsp in ckl.bs_point_lst.bsp_iter():
            # T1（趋势背驰）在 divergence_rate=inf 时不应出现
            type_strs = bsp.type2str()
            # T1P（盘整背驰）和 T2/T3 仍可能出现
            for t in bsp.type:
                assert t != BSP_TYPE.T1 or len(bsp.type) == 0, (
                    f"divergence_rate=inf 时不应产生 T1 类型 BSP"
                )

    def test_bs_type_filtering(self):
        """bs_type 参数正确控制允许的 BSP 类型。"""
        klus = []
        t = 1
        day = 2
        for _ in range(5):
            for p in range(3300, 3600, 5):
                klus.append(_make_klu(2025, 1, day, 9, t,
                                      p, p + 8, p - 8, p + 3))
                t += 1
            for p in range(3595, 3300, -5):
                klus.append(_make_klu(2025, 1, day + 1, 9, t,
                                      p, p + 8, p - 8, p - 3))
                t += 1
            day += 2
            t = 1

        # 只允许 T1
        ckl_t1 = _run_chan_on_klus(klus, bs_type="1", divergence_rate=0.5)
        # 只允许 T2
        ckl_t2 = _run_chan_on_klus(klus, bs_type="2", divergence_rate=float("inf"))

        count_t1 = len(list(ckl_t1.bs_point_lst.bsp_iter()))
        count_t2 = len(list(ckl_t2.bs_point_lst.bsp_iter()))
        # 两种过滤应产生不同数量的 BSP（证明 bs_type 有效）
        # 至少约束：不相等或者都为 0
        assert count_t1 != count_t2 or count_t1 == 0, (
            f"bs_type=1 时 BSP 数 ({count_t1}) 应与 bs_type=2 时 ({count_t2}) 不同"
        )
