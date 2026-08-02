"""T3Scorer: 三类买卖点专用评分器。

三买/三卖逻辑与一买/二买完全不同——关注的是中枢突破和回抽确认。

评分维度:
  1. 中枢突破有效性 — 离开中枢是否充分
  2. 回抽确认 — 回抽是否不破中枢 (不回到中枢内部)
  3. 离开段启动确认 — 笔是否已确认 (再启动)
"""

from __future__ import annotations

from .base import SignalScorer
from signal_core.models import SignalEvent, SignalDirection, SignalState


class T3Scorer(SignalScorer):
    """三类买卖点专用评分器。"""

    def __init__(self, version: str = "0.1.0") -> None:
        super().__init__("chan_t3", version)

    def _score_structural(
        self, event: SignalEvent
    ) -> tuple[float, dict[str, float], tuple[str, ...]]:
        features = event.features
        score = 1.0
        components: dict[str, float] = {}
        blockers: list[str] = []

        # ── 1. 中枢突破有效性 ──
        zs_h = event.zs_high
        zs_l = event.zs_low
        boundary_price = (
            event.structural_price
            if event.structural_price is not None
            else event.reference_price
        )
        components["structural_price_available"] = (
            1.0 if event.structural_price is not None else 0.0
        )

        if zs_h is None or zs_l is None:
            # 尝试从 features.zs_height 回退 (中枢高度已知但范围未知)
            zs_h_feat = self._safe_get(features, "zs_height")
            if self._is_number(zs_h_feat) and float(zs_h_feat) > 0:
                # 只能用 zs_height 做较弱的质量判断, 不做突破判断
                components["zs_break"] = 0.5
                # 不阻断, 只降分
                score -= 0.15
            else:
                blockers.append("zs_range_unavailable")
                components["zs_break"] = 0.0
                score -= 0.5
        elif boundary_price <= 0:
            blockers.append("invalid_reference_price")
            components["zs_break"] = 0.0
            score -= 0.5
        else:
            if event.direction == SignalDirection.LONG:
                # 三买: 应在中枢上方
                if boundary_price <= zs_h:
                    blockers.append("not_above_zs")
                    components["zs_break"] = 0.0
                    score -= 0.5
                else:
                    margin = (boundary_price - zs_h) / zs_h
                    # margin 0%~1% → 0.4, 1%~3% → 0.7, >3% → 1.0
                    if margin < 0.005:
                        components["zs_break"] = 0.3
                    elif margin < 0.01:
                        components["zs_break"] = 0.5
                    elif margin < 0.03:
                        components["zs_break"] = 0.8
                    else:
                        components["zs_break"] = 1.0
            else:
                # 三卖: 应在中枢下方
                if boundary_price >= zs_l:
                    blockers.append("not_below_zs")
                    components["zs_break"] = 0.0
                    score -= 0.5
                else:
                    margin = (zs_l - boundary_price) / zs_l
                    if margin < 0.005:
                        components["zs_break"] = 0.3
                    elif margin < 0.01:
                        components["zs_break"] = 0.5
                    elif margin < 0.03:
                        components["zs_break"] = 0.8
                    else:
                        components["zs_break"] = 1.0

        # ── 2. 回抽再启动力度 ──
        bi_amp = self._safe_get(features, "bi_amp")
        if bi_amp is None:
            bi_amp = self._safe_get(features, "bsp3_bi_amp")

        if bi_amp is None or not self._is_number(bi_amp):
            components["return_strength"] = 0.5
        else:
            amp = float(bi_amp)
            if amp < 0.001:
                components["return_strength"] = 0.2
                score -= 0.2
            elif amp < 0.004:
                components["return_strength"] = 0.5
            elif amp < 0.008:
                components["return_strength"] = 0.8
            else:
                components["return_strength"] = 1.0

        # ── 3. 离开段再启动确认 ──
        if event.state == SignalState.CANDIDATE:
            components["restart_confirmed"] = 0.4
            score -= 0.25
        else:
            components["restart_confirmed"] = 1.0

        score = max(0.0, min(1.0, score))
        return score, components, tuple(blockers)
