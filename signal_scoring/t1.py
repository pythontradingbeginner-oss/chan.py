"""T1Scorer: 一类买卖点 (趋势背驰) 和盘整背驰的专用评分器。

评分维度:
  1. 背驰度 — divergence_rate 越低越好 (强背驰)
  2. 中枢数 — 至少经历 1 个中枢 (T1P 盘背无此要求)
  3. 笔确认状态 — 确认笔得分高于虚拟笔
  4. 笔振幅 — 过滤振幅过小的假信号
"""

from __future__ import annotations

from .base import SignalScorer
from signal_core.models import SignalEvent, SignalState


class T1Scorer(SignalScorer):
    """一类买卖点 + 盘整背驰评分器。"""

    def __init__(self, version: str = "0.1.0") -> None:
        super().__init__("chan_t1", version)

    def _score_structural(
        self, event: SignalEvent
    ) -> tuple[float, dict[str, float], tuple[str, ...]]:
        features = event.features
        score = 1.0
        components: dict[str, float] = {}
        blockers: list[str] = []

        # ── 1. 背驰度 (权重最高) ──
        div_rate = self._safe_get(features, "divergence_rate")
        if div_rate is None:
            blockers.append("divergence_unavailable")
            score -= 0.5
            components["divergence"] = 0.0
        elif self._is_number(div_rate):
            div = float(div_rate)
            if div > 1.2:
                blockers.append("no_divergence")
                score -= 0.4
                components["divergence"] = 0.0
            elif div > 1.0:
                components["divergence"] = 0.4
                score -= 0.2
            elif div > 0.7:
                components["divergence"] = 0.7
            elif div > 0.4:
                components["divergence"] = 0.9
            else:
                components["divergence"] = 1.0   # 强烈背驰
        else:
            components["divergence"] = 0.0
            score -= 0.5

        # ── 2. 中枢数 ──
        zs_cnt = self._safe_get(features, "zs_cnt")
        if zs_cnt is None or not self._is_number(zs_cnt):
            if event.primary_bsp == "1p":
                components["zs_cnt"] = 0.5  # 盘背不要求中枢
            else:
                components["zs_cnt"] = 0.0
                score -= 0.2
        else:
            zs = int(zs_cnt)
            if zs == 0:
                if event.primary_bsp == "1p":
                    components["zs_cnt"] = 0.5
                else:
                    components["zs_cnt"] = 0.0
                    score -= 0.2
            elif zs == 1:
                components["zs_cnt"] = 0.8
            elif zs >= 2:
                components["zs_cnt"] = 1.0  # 多中枢背驰
            else:
                components["zs_cnt"] = 0.5

        # ── 3. 笔确认状态 ──
        if event.state == SignalState.CANDIDATE:
            components["bi_confirmed"] = 0.3
            score -= 0.3
        else:
            components["bi_confirmed"] = 1.0

        # ── 4. 笔振幅 (过滤振幅过小的假信号) ──
        bi_amp = self._safe_get(features, "bi_amp")
        if bi_amp is None or not self._is_number(bi_amp):
            components["bi_amp"] = 0.5
        else:
            amp = float(bi_amp)
            if amp < 0.001:
                components["bi_amp"] = 0.0
                score -= 0.15
            elif amp < 0.003:
                components["bi_amp"] = 0.4
            elif amp < 0.008:
                components["bi_amp"] = 0.7
            else:
                components["bi_amp"] = 1.0

        score = max(0.0, min(1.0, score))
        return score, components, tuple(blockers)
