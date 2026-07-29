"""区间套确认模块 —— 本级别 BSP 需次级别同向 BSP 确认。

缠论区间套原理:
  本级别某笔尾出现买点 → 在次级别的同一笔范围内检查是否也有同向买点
  → 次级别也背驰 (区间套) → 本级别买点确认度更高
  → 次级别无背驰 → 本级别买点仍有效但未达区间套级别

这个过滤使用的是「在决策时刻已经可见」的次级别 BSP ——
不依赖次级别未完成的 K 线，因此不存在未来函数问题。

用法:
    from chan_futures.interval_filter import IntervalConfirmer
    ic = IntervalConfirmer()
    result = ic.confirm(chan, bsp_bi_idx, bsp_direction, lv_idx=0)
    if result.confirmed:
        print(f"区间套确认: {result.detail}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from Common.CEnum import BSP_TYPE, KL_TYPE


@dataclass(frozen=True)
class IntervalResult:
    confirmed: bool       # 次级别是否也出现了同向 BSP
    sub_bsp_count: int    # 次级别同向 BSP 数量
    sub_bsp_types: str    # 次级别 BSP 类型 (逗号分隔)
    detail: str           # 可读描述


class IntervalConfirmer:
    """检查本级别 BSP 是否被次级别同向 BSP 确认。

    原理:
      1. 取本级别 BSP 所在的笔的起始 ~ 结束 K 线
      2. 在此 K 线范围内查找次级别的同向 BSP
      3. 如果找到 → confirmed=True (区间套确认)
    """

    def __init__(
        self,
        *,
        require_sure_bi: bool = True,
        sub_lv_bsp_types: frozenset[str] = frozenset({"1", "1p", "2"}),
    ) -> None:
        """
        Args:
            require_sure_bi: True 时只接受已确认的笔上的 BSP
            sub_lv_bsp_types: 次级别接受的 BSP 类型
        """
        self._require_sure_bi = require_sure_bi
        self._sub_bsp_types = sub_lv_bsp_types

    def confirm(
        self,
        chan,
        bsp_bi_idx: int,
        bsp_direction: str,  # "long" | "short"
        *,
        lv_idx: int = 0,
    ) -> IntervalResult:
        """检查区间套。

        Args:
            chan: CChan 实例 (必须至少 2 个级别)
            bsp_bi_idx: 本级别 BSP 所在笔的 idx
            bsp_direction: "long" (买点→次级别需要卖点确认) 或 "short"
            lv_idx: 本级别在 lv_list 中的索引

        Returns:
            IntervalResult
        """
        # ── 检查是否有次级别 ──
        try:
            lv_list = chan.lv_list
            if not isinstance(lv_list, (list, tuple)):
                return IntervalResult(False, 0, "", "lv_list 不可用")
            if lv_idx + 1 >= len(lv_list):
                return IntervalResult(False, 0, "", "无次级别")
            sub_lv = lv_list[lv_idx + 1]
        except (AttributeError, IndexError, TypeError):
            return IntervalResult(False, 0, "", "无法获取次级别")

        # ── 取本级别 BSP 所在的笔 ──
        try:
            cur_data = chan[lv_idx]
            bi_list = cur_data.bi_list
            if bsp_bi_idx < 0 or bsp_bi_idx >= len(bi_list):
                return IntervalResult(False, 0, "", f"bi_idx={bsp_bi_idx} 超出范围")
            target_bi = bi_list[bsp_bi_idx]
        except (KeyError, IndexError, AttributeError):
            return IntervalResult(False, 0, "", "无法获取目标笔")

        if self._require_sure_bi and not target_bi.is_sure:
            return IntervalResult(False, 0, "", "笔未确认")

        # ── 获取笔范围内的所有 K 线 ──
        begin_klu = target_bi.get_begin_klu()
        end_klu = target_bi.get_end_klu()

        if begin_klu is None or end_klu is None:
            return IntervalResult(False, 0, "", "笔的起止 K 线为空")

        begin_idx = begin_klu.idx if hasattr(begin_klu, "idx") else int(begin_klu.time.to_str())
        end_idx = end_klu.idx if hasattr(end_klu, "idx") else int(end_klu.time.to_str())

        # ── 获取次级别数据 ──
        try:
            sub_data = chan[sub_lv]
            sub_bsp_list = sub_data.bs_point_lst
        except (KeyError, TypeError, AttributeError):
            return IntervalResult(False, 0, "", "次级别数据不可用")

        # ── 在次级别中查找同向 BSP ──
        # 买点 (long): 本级别笔下降 → 次级别笔尾出现买点 (is_buy=True)
        # 卖点 (short): 本级别笔上升 → 次级别笔尾出现卖点 (is_buy=False)
        is_buy = bsp_direction == "long"

        matching_bsps: list[str] = []
        for sub_bsp in sub_bsp_list.bsp_iter():
            if sub_bsp.is_buy != is_buy:
                continue

            # 次级别 BSP 的 K 线必须在笔范围内
            sub_klu = sub_bsp.klu
            if sub_klu is None:
                continue
            sub_idx = sub_klu.idx if hasattr(sub_klu, "idx") else 0

            # 检查次级别 K 线是否在本级别笔的范围内
            if not (begin_idx <= sub_idx <= end_idx):
                continue

            # 检查 BSP 类型是否接受
            bsp_type_str = sub_bsp.type2str()
            if not any(t in self._sub_bsp_types for t in [bsp_type_str]):
                continue

            matching_bsps.append(bsp_type_str)

        if matching_bsps:
            types_str = ",".join(sorted(set(matching_bsps)))
            return IntervalResult(
                confirmed=True,
                sub_bsp_count=len(matching_bsps),
                sub_bsp_types=types_str,
                detail=f"区间套确认: 次级别 {len(matching_bsps)} 个同向 BSP ({types_str})",
            )
        else:
            return IntervalResult(
                confirmed=False,
                sub_bsp_count=0,
                sub_bsp_types="",
                detail="无次级别同向 BSP",
            )
