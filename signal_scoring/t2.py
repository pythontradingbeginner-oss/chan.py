"""T2Scorer: 二类买卖点 + 类二买卖点专用评分器。

评分维度:
  1. 回撤幅度 — 回撤率在 0.382~0.618 之间最佳 (不破一买)
  2. 一买锚点有效性 — 回撤不能跌破一买最低点
  3. 笔确认状态 — 确认笔得分高于虚拟笔
"""

from __future__ import annotations

from .base import SignalScorer
from signal_core.models import SignalEvent, SignalDirection, SignalState


class T2Scorer(SignalScorer):
    """二类买卖点 + 类二买卖点评分器。"""

    def __init__(self, version: str = "0.1.0") -> None:
        super().__init__("chan_t2", version)

    def _score_structural(
        self, event: SignalEvent
    ) -> tuple[float, dict[str, float], tuple[str, ...]]:
        features = event.features
        score = 1.0
        components: dict[str, float] = {}
        blockers: list[str] = []

        # ── 1. 回撤幅度 ──
        rr = self._safe_get(features, "retrace_rate")
        if rr is None or not self._is_number(rr):
            blockers.append("retrace_rate_unavailable")
            components["retrace"] = 0.0
            score -= 0.5
        else:
            retrace = float(rr)
            if retrace <= 0.15:
                components["retrace"] = 0.4   # 回撤太浅, 可能没完成
                score -= 0.2
            elif retrace <= 0.382:
                components["retrace"] = 1.0   # 浅回撤, 强势
            elif retrace <= 0.50:
                components["retrace"] = 0.9   # 正常
            elif retrace <= 0.618:
                components["retrace"] = 0.8   # 正常偏深
            elif retrace <= 0.80:
                components["retrace"] = 0.5   # 较深
                score -= 0.2
            else:
                components["retrace"] = 0.2   # 逼近一买, 高风险
                score -= 0.35

        # ── 2. 一买锚点有效性 ──
        # 注: bi_begin_price 对于 T2 不是真正的 BSP1 锚点, 仅做参考
        # T2 买点: 回撤不能跌破前底 (bi_begin_price≈前底)
        # T2 卖点: 反弹不能突破前顶 (bi_begin_price≈前顶)
        if event.bi_begin_price is not None and event.reference_price > 0:
            begin = float(event.bi_begin_price)
            ref = float(event.reference_price)
            if event.direction == SignalDirection.LONG:
                # 买点: ref_price 如果在 begin 上方, 说明回撤未创新低
                anchor_ok = ref > begin
            else:
                # 卖点: ref_price 如果在 begin 下方, 说明反弹未创新高
                anchor_ok = ref < begin
            if anchor_ok:
                components["bsp1_anchor"] = 1.0
            else:
                # 锚点失效 → 仅降分, 不阻断 (因为 bi_begin_price 不完全等于 BSP1 失效位)
                components["bsp1_anchor"] = 0.3
                score -= 0.15
        else:
            components["bsp1_anchor"] = 0.5  # 无数据

        # ── 3. 笔确认状态 ──
        if event.state == SignalState.CANDIDATE:
            components["bi_confirmed"] = 0.3
            score -= 0.3
        else:
            components["bi_confirmed"] = 1.0

        score = max(0.0, min(1.0, score))
        return score, components, tuple(blockers)
