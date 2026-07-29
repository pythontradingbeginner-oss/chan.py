"""评分器基类与通用工具。

设计:
  - 评分器是纯函数: SignalEvent → SignalAssessment
  - 构造后不可变: 评分在 __init__ 内完成, 返回 frozen dataclass
  - structural_score: 结构质量 0~1
  - predictive_score: 历史胜率 (后续 ML 模型填充, 当前为 None)
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalEvent,
    SignalState,
)


class SignalScorer(ABC):
    """评分器抽象基类。

    子类只需实现 _score_structural(), 返回 (总评分, 子项得分, 硬阻断原因)。
    assess() 方法负责组装 SignalAssessment。
    """

    def __init__(self, scorer_id: str, version: str = "0.1.0") -> None:
        self.scorer_id = scorer_id
        self.version = version

    # ── 公开接口 ──────────────────────────────

    def assess(self, event: SignalEvent) -> SignalAssessment:
        """主入口: 评估一个 SignalEvent 并返回不可变的 SignalAssessment。"""
        # 非活跃信号直接跳过
        if event.state in (SignalState.INVALIDATED, SignalState.EXPIRED):
            return self._build(event, structural_score=0.0, predictive_score=None,
                               hard_blockers=("event_not_active",), component_scores={})

        score, components, blockers = self._score_structural(event)
        return self._build(event, score, None, blockers, components)

    # ── 子类必须实现 ──────────────────────────

    @abstractmethod
    def _score_structural(
        self, event: SignalEvent
    ) -> tuple[float, dict[str, float], tuple[str, ...]]:
        """评估结构质量。

        Returns:
            (overall_score 0~1, 子项得分明细, 硬阻断原因码列表)
        """
        ...

    # ── 内部工具 ──────────────────────────────

    @staticmethod
    def _safe_get(features: dict[str, Any], key: str, default: Any = None) -> Any:
        """安全从 features 取值, 缺失返回 default。"""
        return features.get(key, default)

    @staticmethod
    def _is_number(val: Any) -> bool:
        return isinstance(val, (int, float)) and not isinstance(val, bool)

    def _build(
        self,
        event: SignalEvent,
        structural_score: float | None,
        predictive_score: float | None,
        hard_blockers: tuple[str, ...],
        component_scores: dict[str, float],
    ) -> SignalAssessment:
        grade = ScoreGrade.from_score(structural_score) if structural_score is not None else ScoreGrade.WEAK
        return SignalAssessment(
            assessment_id=str(uuid.uuid4())[:12],
            event_id=event.event_id,
            scorer_id=self.scorer_id,
            scorer_version=self.version,
            structural_score=structural_score,
            predictive_score=predictive_score,
            grade=grade,
            hard_blockers=hard_blockers,
            component_scores=component_scores,
            computed_at=datetime.now(),
        )


# ── 评分器注册表 ──────────────────────────

from .t1 import T1Scorer
from .t2 import T2Scorer
from .t3 import T3Scorer

_SCORER_REGISTRY: dict[str, SignalScorer] = {
    "1":  T1Scorer(),
    "1p": T1Scorer(),
    "2":  T2Scorer(),
    "2s": T2Scorer(),
    "3a": T3Scorer(),
    "3b": T3Scorer(),
}


def assess_event(
    event: SignalEvent, *, scorer_id: str | None = None
) -> SignalAssessment:
    """便捷函数: 根据 event.primary_bsp 自动选择评分器并评估。

    用法:
        event = extractor.extract(bsp, chan=chan, ...)
        assessment = assess_event(event)
    """
    scorer: SignalScorer | None = None
    if scorer_id is not None:
        scorer = _SCORER_REGISTRY.get(scorer_id)
    if scorer is None:
        scorer = _SCORER_REGISTRY.get(event.primary_bsp)
    if scorer is None:
        scorer = T1Scorer()  # fallback
    return scorer.assess(event)
