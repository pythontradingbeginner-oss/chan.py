"""多级别父级别趋势过滤器 —— 将父级别线段方向作为信号确认条件。

缠论核心理念：大级别定方向，小级别找买点。
  - 60 分钟上升线段 → 15 分钟只接受 LONG 信号
  - 60 分钟下降线段 → 15 分钟只接受 SHORT 信号
  - 60 分钟无明确方向 → 两个方向都接受

这是一种"纯当下可用"的过滤 —— 不依赖父级别已完成的信号，
只取父级别的当前线段方向。

用法:
    from chan_futures.parent_filter import ParentTrendFilter
    ptf = ParentTrendFilter(lv_idx=0, parent_lv_name='K_60M')
    grade_adjust = ptf.filter(chan, signal_direction='long')
    # 如果 grade_adjust == 'reject', 信号被拒绝
    # 如果 grade_adjust == 'downgrade', 信号降级
    # 如果 grade_adjust == 'pass', 信号正常通过
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from Common.CEnum import KL_TYPE


class ParentTrendAction(StrEnum):
    PASS = "pass"            # 父级别趋势同向 → 通过
    REJECT = "reject"        # 父级别趋势反向 → 拒绝
    DOWNGRADE = "downgrade"  # 父级别无方向 → 降一级 (e.g. IDEAL→STANDARD)


@dataclass(frozen=True)
class ParentTrendResult:
    action: ParentTrendAction
    parent_direction: str    # "up" / "down" / "unknown"
    parent_seg_idx: int      # 父级别最后线段的 idx
    detail: str              # 可读描述


class ParentTrendFilter:
    """根据父级别的当前线段方向过滤子级别信号。

    逻辑:
        父级别上升 → 只接受 LONG   (SHORT → REJECT)
        父级别下降 → 只接受 SHORT  (LONG  → REJECT)
        父级别未知 → 两个方向接受但降级 (DOWNGRADE)

    这避免了"在下跌趋势中抄底"的行为。
    """

    def __init__(
        self,
        *,
        parent_lv: KL_TYPE | None = None,
        require_confirmed_seg: bool = False,
        downgrade_on_unknown: bool = True,
    ) -> None:
        """
        Args:
            parent_lv: 父级别 KL_TYPE。None 则从 chan.lv_list 推断 (lv_idx-1)
            require_confirmed_seg: True 时只接受 is_sure=True 的线段方向
            downgrade_on_unknown: True 时无方向信号降级而非通过
        """
        self._parent_lv = parent_lv
        self._require_confirmed = require_confirmed_seg
        self._downgrade_on_unknown = downgrade_on_unknown

    def filter(
        self,
        chan,
        signal_direction: str,  # "long" | "short"
        *,
        lv_idx: int = 0,
    ) -> ParentTrendResult:
        """检查父级别趋势是否支持当前信号。

        Args:
            chan: CChan 实例 (必须包含至少 2 个级别)
            signal_direction: "long" 或 "short"
            lv_idx: 当前级别的索引

        Returns:
            ParentTrendResult 包含过滤决策。
        """
        # ── 解析父级别 ──
        if self._parent_lv is not None:
            parent_lv = self._parent_lv
        else:
            # 从 lv_list 推断: 父级别 = lv_list[lv_idx - 1]
            try:
                parent_lv = chan.lv_list[lv_idx - 1] if lv_idx > 0 else chan.lv_list[0]
            except (AttributeError, IndexError):
                return ParentTrendResult(
                    action=ParentTrendAction.PASS,
                    parent_direction="unknown",
                    parent_seg_idx=-1,
                    detail="无法解析父级别 → 通过",
                )
            if parent_lv == chan.lv_list[lv_idx] if lv_idx < len(chan.lv_list) else False:
                # 单级别 → 通过
                return ParentTrendResult(
                    action=ParentTrendAction.PASS,
                    parent_direction="unknown",
                    parent_seg_idx=-1,
                    detail="单级别 → 通过",
                )

        # ── 取父级别数据 ──
        try:
            parent_data = chan[parent_lv]
        except (KeyError, TypeError):
            return ParentTrendResult(
                action=ParentTrendAction.PASS,
                parent_direction="unknown",
                parent_seg_idx=-1,
                detail="父级别数据不可用 → 通过",
            )

        seg_list = parent_data.seg_list

        # ── 取最后一条已确认线段 ──
        last_confirmed = None
        for seg in reversed(seg_list.lst):
            if seg.is_sure:
                last_confirmed = seg
                break

        if last_confirmed is None and self._require_confirmed:
            return ParentTrendResult(
                action=ParentTrendAction.DOWNGRADE if self._downgrade_on_unknown else ParentTrendAction.PASS,
                parent_direction="unknown",
                parent_seg_idx=-1,
                detail="父级别无已确认线段",
            )

        # 如果没有已确认线段但不需要确认 → 取最后一条线段
        target_seg = last_confirmed
        if target_seg is None and seg_list.lst:
            target_seg = seg_list.lst[-1]

        if target_seg is None:
            return ParentTrendResult(
                action=ParentTrendAction.DOWNGRADE if self._downgrade_on_unknown else ParentTrendAction.PASS,
                parent_direction="unknown",
                parent_seg_idx=-1,
                detail="父级别无线段 → 通过",
            )

        # ── 判断方向 ──
        # CBi.dir: BI_DIR.UP (=1) / BI_DIR.DOWN (=2)
        seg_dir = target_seg.dir
        parent_direction = "up" if seg_dir.value == 1 else "down"

        # long 信号 → 父级别必须是 up
        if signal_direction == "long" and parent_direction == "up":
            return ParentTrendResult(
                action=ParentTrendAction.PASS,
                parent_direction=parent_direction,
                parent_seg_idx=target_seg.idx,
                detail=f"父级别上升 → 多信号通过 (seg_idx={target_seg.idx})",
            )
        elif signal_direction == "short" and parent_direction == "down":
            return ParentTrendResult(
                action=ParentTrendAction.PASS,
                parent_direction=parent_direction,
                parent_seg_idx=target_seg.idx,
                detail=f"父级别下降 → 空信号通过 (seg_idx={target_seg.idx})",
            )

        # 方向相反 → REJECT
        return ParentTrendResult(
            action=ParentTrendAction.REJECT,
            parent_direction=parent_direction,
            parent_seg_idx=target_seg.idx,
            detail=f"父级别方向={parent_direction}, 信号方向={signal_direction} → 拒绝",
        )
