"""策略入口策略层 —— 消费 SignalAssessment，产出 SignalDecision。

职责：
  1. 将「评估结果」转换为「策略是否接受」
  2. 基于信号结构和评估结果生成入场计划（入场价、止损价、失效位）
  3. 记录所有决策（包括 rejected）供复盘

设计原则：
  - 入口策略不直接依赖 CBS_Point / CBi / CChan
  - 只消费 SignalEvent + SignalAssessment 纯数据
  - 支持多策略并行评估同一个信号（策略 A 接受，策略 B 拒绝）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
    SignalState,
)


# ═══════════════════════════════════════════
# EntryPolicy — 入口规则框架
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class EntryPolicyConfig:
    """入口策略配置 — 定义接受哪些信号的规则组合。

    每个 condition 是可选的 Callable:
        (SignalEvent, SignalAssessment) → bool
    所有条件 AND 连接。
    """
    policy_id:          str           = "default"
    min_grade:          ScoreGrade    = ScoreGrade.STANDARD
    require_confirmed:  bool          = True
    accepted_bsp_types: frozenset[str] = frozenset({"1", "1p", "2", "2s", "3a", "3b"})
    long_only:          bool          = False
    # 额外自定义条件
    extra_conditions:   tuple[Callable, ...] = ()
    # 仓位与止损
    default_stop_ratio: float         = 0.02    # 默认止损比例 2%
    default_size:       float         = 1.0


class EntryPolicy:
    """根据配置决定是否接受信号，并生成 SignalDecision + 入场计划。

    用法:
        policy = EntryPolicy(EntryPolicyConfig(min_grade=ScoreGrade.STANDARD))
        decision = policy.evaluate(event, assessment)
        if decision.accepted:
            place_order(decision.entry_price_hint)
    """

    def __init__(self, config: EntryPolicyConfig | None = None) -> None:
        self.cfg = config or EntryPolicyConfig()

    def evaluate(
        self,
        event: SignalEvent,
        assessment: SignalAssessment,
    ) -> SignalDecision:
        """对信号事件 + 评估结果进行入口决策。"""
        reasons: list[str] = []

        # 1. 信号生命周期
        if self.cfg.require_confirmed and event.state != SignalState.CONFIRMED:
            reasons.append("signal_not_confirmed")

        # 2. BSP 类型
        if event.primary_bsp not in self.cfg.accepted_bsp_types:
            reasons.append(f"bsp_type_rejected:_{event.primary_bsp}")

        # 3. 方向限制
        if self.cfg.long_only and event.direction == SignalDirection.SHORT:
            reasons.append("short_disabled")

        # 4. 最低评分
        grade_order = {ScoreGrade.IDEAL: 3, ScoreGrade.STANDARD: 2, ScoreGrade.WEAK: 1}
        if grade_order.get(assessment.grade, 0) < grade_order.get(self.cfg.min_grade, 0):
            reasons.append(f"grade_below_min:_{assessment.grade.value}")

        # 5. 硬阻断
        for blocker in assessment.hard_blockers:
            reasons.append(f"hard_blocker:{blocker}")

        # 6. 自定义条件
        for cond in self.cfg.extra_conditions:
            try:
                if not cond(event, assessment):
                    reasons.append(f"extra_condition_failed:{cond.__name__}")
            except Exception:
                reasons.append("extra_condition_error")

        # ── 判定 ──
        accepted = len(reasons) == 0

        # ── 入场计划 ──
        entry_price_hint = event.reference_price if accepted else None
        invalidation_price = _calc_invalidation(event) if accepted else None
        initial_stop = _calc_initial_stop(event, self.cfg.default_stop_ratio) if accepted else None

        return SignalDecision(
            decision_id=str(uuid.uuid4())[:12],
            event_id=event.event_id,
            policy_id=self.cfg.policy_id,
            accepted=accepted,
            reason_codes=tuple(reasons),
            entry_price_hint=entry_price_hint,
            invalidation_price=invalidation_price,
            initial_stop_price=initial_stop,
            position_size_hint=self.cfg.default_size if accepted else None,
            decided_at=datetime.now(),
        )


# ═══════════════════════════════════════════
# 入场计划辅助
# ═══════════════════════════════════════════

def _calc_invalidation(event: SignalEvent) -> float | None:
    """计算信号失效价：跌破/突破此价则信号作废。

    - 买点: 笔起始价（前低/底分型低点）—— 跌破即失效
    - 卖点: 笔起始价（前高/顶分型高点）—— 突破即失效
    """
    if event.bi_begin_price is None:
        return None
    return float(event.bi_begin_price)


def _calc_initial_stop(
    event: SignalEvent,
    default_ratio: float,
) -> float:
    """基于结构锚点的初始止损价。

    优先级: zs_high/zs_low > bi_begin_price > 固定比例
    """
    ref = event.reference_price
    if event.direction == SignalDirection.LONG:
        # 买点止损: 优先用中枢下沿，其次是笔起始价
        if event.zs_low is not None and event.zs_low < ref:
            return float(event.zs_low)
        if event.bi_begin_price is not None and event.bi_begin_price < ref:
            return float(event.bi_begin_price)
        return ref * (1 - default_ratio)
    else:
        # 卖点止损: 优先用中枢上沿
        if event.zs_high is not None and event.zs_high > ref:
            return float(event.zs_high)
        if event.bi_begin_price is not None and event.bi_begin_price > ref:
            return float(event.bi_begin_price)
        return ref * (1 + default_ratio)


# ═══════════════════════════════════════════
# 多策略评估器
# ═══════════════════════════════════════════

class MultiPolicyEvaluator:
    """对同一信号事件用多个入口策略并行评估。

    用法:
        evaluator = MultiPolicyEvaluator({
            "strict":  EntryPolicy(EntryPolicyConfig(min_grade=ScoreGrade.IDEAL)),
            "default": EntryPolicy(EntryPolicyConfig(min_grade=ScoreGrade.STANDARD)),
        })
        for policy_id, decision in evaluator.evaluate_all(event, assessment):
            if decision.accepted:
                signals_by_policy[policy_id].append(decision)
    """

    def __init__(self, policies: dict[str, EntryPolicy]) -> None:
        self._policies = policies

    def evaluate_all(
        self, event: SignalEvent, assessment: SignalAssessment,
    ) -> list[tuple[str, SignalDecision]]:
        results: list[tuple[str, SignalDecision]] = []
        for pid, policy in self._policies.items():
            decision = policy.evaluate(event, assessment)
            results.append((pid, decision))
        return results

    @property
    def policy_ids(self) -> list[str]:
        return list(self._policies.keys())
