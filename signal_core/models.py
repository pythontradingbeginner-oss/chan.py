"""信号规范化数据模型 —— 四层不可变 dataclass。

四层:
  SignalEvent      - 客观事实: 某时刻系统观察到了什么形态
  SignalAssessment - 评分解释: 某版本评分器对此事件的评价
  SignalDecision   - 策略决策: 某策略是否接受此信号 + 入场计划
  SignalRelation   - 多级别共振关系 (独立存储, 不在 event 内耦合)

设计原则:
  - frozen=True + slots=True → 构造后不可变, 内存紧凑
  - 不持有 CBi / CKLine_Unit / CChan 引用 → 纯标量快照
  - signal_key 稳定跨 revision 不变, event_id 每次新生成
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


# ═══════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════

class SignalState(StrEnum):
    """信号生命周期状态。

    缠论买卖点的天然生命周期:
      - 下跌笔新低 → CANDIDATE (一买候选)
      - 反弹后笔确认 → CONFIRMED (一买确定)
      - 后续跌破新低 → INVALIDATED (一买失效)
      - 超过 N 根 K 线未确认 → EXPIRED

    每个状态变更都生成一条新 SignalEvent (revision 递增),
    不覆盖旧记录, 从而保留完整的信号演化历史。
    """
    CANDIDATE    = "candidate"      # 出现但笔/段未确认
    CONFIRMED    = "confirmed"      # 后续笔/段确认, 信号定稿
    INVALIDATED  = "invalidated"    # 后续被否定 (如跌破一买最低点)
    EXPIRED      = "expired"        # 超时未确认


class SignalDirection(StrEnum):
    LONG  = "long"
    SHORT = "short"


class ScoreGrade(StrEnum):
    """信号质量分档, 由 structural_score 映射而来。

    IDEAL    - 形态标准, 关键指标都达标
    STANDARD - 形态满足基本要求
    WEAK     - 形态勉强成立, 某关键指标边缘
    """
    IDEAL    = "ideal"
    STANDARD = "standard"
    WEAK     = "weak"

    @classmethod
    def from_score(cls, score: float) -> ScoreGrade:
        if score >= 0.80:
            return cls.IDEAL
        if score >= 0.55:
            return cls.STANDARD
        return cls.WEAK

    @classmethod
    def min_grade_filter(cls, min_grade: ScoreGrade):
        """返回接受 ≥ min_grade 的 lambda — 研究阶段快速过滤用。"""
        order = [cls.IDEAL, cls.STANDARD, cls.WEAK]
        allowed = set(order[: order.index(min_grade) + 1])
        return lambda assessment: assessment.grade in allowed


# ═══════════════════════════════════════════
# 层 1: SignalEvent — 客观事实
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SignalEvent:
    """客观记录: 在某个时刻, 系统观察到了什么。

    关键设计:
      - available_at: 策略真正能「看见」此信号的最早时刻。
        同一 bar 收盘后信号立即可见, 因此 available_at = bar_end_time。
        这个字段是未来函数防护的核心——上级别信号必须在 child 决策时已可见。
      - signal_key: 同一个信号生命周期的稳定 ID。event_id 每次 revision 新生成。
      - 除 features 外, 所有字段都是不可变的标量值。
    """

    # ── 身份 ──
    event_id:    str           # 全局唯一 ID, 每次 revision 新生成
    signal_key:  str           # 同一信号生命周期的稳定标识 (跨 revision 不变)
    revision:    int           # 第几次更新 (0=首次出现)

    # ── 品种与周期 ──
    symbol:      str           # 如 "RB"
    contract:    str           # 如 "RB_MAIN"
    timeframe:   str           # "1m" / "15m" / "60m" / "day"

    # ── 时间 (未来函数防护核心) ──
    bar_end_time: datetime     # 信号形态所在的 K 线收盘时刻
    available_at: datetime     # 策略最早能看见此信号的时刻

    # ── 生命周期 ──
    state:       SignalState

    # ── 方向与类型 ──
    direction:   SignalDirection
    primary_bsp: str           # "1" / "1p" / "2" / "2s" / "3a" / "3b" — 主类型
    bsp_types:   tuple[str, ...]  # 若同时是 T2+T3B, 则为 ("2","3b")

    # ── 价格 ──
    reference_price: float     # 信号所在笔尾 K 线收盘价

    # ── 结构锚点 (出场止损/止盈定位用) ──
    bi_idx:          int       # 本级别内笔编号 (非全局唯一)
    seg_idx:         int | None  # 本级别内线段编号
    bi_begin_price:  float | None  # 该笔起始价 = 前一个底/顶分型极值
    zs_high:         float | None  # 最近中枢上沿
    zs_low:          float | None  # 最近中枢下沿

    # ── 多级别关联 ──
    parent_event_id: str | None  # 上级别关联信号的 event_id

    # ── 特征快照 ──
    feature_schema_version: str    # "v0"
    features:   dict[str, Any]  # 从 CBS_Point.features + 结构计算的标量快照
    chan_version: str           # chan.py 版本标识
    data_run_id:  str           # 数据管线批次标识

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 友好的 dict (用于 DataFrame 或持久化)。"""
        return {
            "event_id":       self.event_id,
            "signal_key":     self.signal_key,
            "revision":       self.revision,
            "symbol":         self.symbol,
            "contract":       self.contract,
            "timeframe":      self.timeframe,
            "bar_end_time":   self.bar_end_time.isoformat(),
            "available_at":   self.available_at.isoformat(),
            "state":          self.state.value,
            "direction":      self.direction.value,
            "primary_bsp":    self.primary_bsp,
            "bsp_types":      ",".join(self.bsp_types),
            "reference_price": self.reference_price,
            "bi_idx":         self.bi_idx,
            "seg_idx":        self.seg_idx,
            "bi_begin_price": self.bi_begin_price,
            "zs_high":        self.zs_high,
            "zs_low":         self.zs_low,
            "parent_event_id": self.parent_event_id,
            "feature_schema_version": self.feature_schema_version,
            "features":       str(self.features),  # JSON 字符串化, 避免 DataFrame object 列
            "chan_version":   self.chan_version,
            "data_run_id":    self.data_run_id,
        }


# ═══════════════════════════════════════════
# 层 2: SignalAssessment — 评分
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SignalAssessment:
    """某个版本评分器对某个 SignalEvent 的解释。

    同一个 SignalEvent 可对应多条 SignalAssessment:
      - chan_t1_v0: structural=0.68, grade=standard
      - chan_t1_v1: structural=0.74, grade=standard
      - ml_continue_v0: predictive=0.61
    这允许后续迭代评分器时保留历史评分, 不覆盖旧数据。
    """

    assessment_id:  str
    event_id:       str

    scorer_id:      str          # "chan_t1_v0" / "chan_t3_v0"
    scorer_version: str          # "0.1.0"

    structural_score: float | None  # 结构质量 0~1
    predictive_score: float | None  # 历史胜率 (后续 ML 填充)

    grade:          ScoreGrade
    hard_blockers:  tuple[str, ...]   # 硬阻断原因码, 如 ("no_divergence",)
    component_scores: dict[str, float]  # 子项得分明细
    computed_at:    datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id":   self.assessment_id,
            "event_id":        self.event_id,
            "scorer_id":       self.scorer_id,
            "scorer_version":  self.scorer_version,
            "structural_score": self.structural_score,
            "predictive_score": self.predictive_score,
            "grade":           self.grade.value,
            "hard_blockers":   "|".join(self.hard_blockers),
            "component_scores": str(self.component_scores),
            "computed_at":     self.computed_at.isoformat(),
        }


# ═══════════════════════════════════════════
# 层 3: SignalDecision — 策略决策
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SignalDecision:
    """某策略对某个信号的接受/拒绝决策 + 入场计划。

    保留了 rejected 的决策——这对复盘非常重要:
      出现信号 → 被某条规则拒绝 → 记录原因 → 后续可分析这条规则是否过于严格。
    """

    decision_id:  str
    event_id:     str
    policy_id:    str           # "rb_15m_bsp_filter_v1"

    accepted:     bool
    reason_codes: tuple[str, ...]  # 原因码

    # ── 以下仅 accepted=True 时有意义 ──
    entry_price_hint:    float | None
    invalidation_price:  float | None  # 失效价 (跌破/突破此价则信号作废)
    initial_stop_price:  float | None
    position_size_hint:  float | None

    decided_at:  datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id":        self.decision_id,
            "event_id":           self.event_id,
            "policy_id":          self.policy_id,
            "accepted":           self.accepted,
            "reason_codes":       "|".join(self.reason_codes),
            "entry_price_hint":   self.entry_price_hint,
            "invalidation_price": self.invalidation_price,
            "initial_stop_price": self.initial_stop_price,
            "position_size_hint": self.position_size_hint,
            "decided_at":         self.decided_at.isoformat(),
        }


# ═══════════════════════════════════════════
# 层 4: SignalRelation — 多级别关联
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SignalRelation:
    """多级别共振/关联关系 — 独立于 event 存储。

    关键约束 (防未来函数):
      上级别信号必须在子级别决策时已经可见。
      即: parent.available_at <= child.available_at
    """

    child_event_id:  str
    parent_event_id: str
    relation_type:   str           # "resonance" / "confirms" / "contains"
    known_at:        datetime      # 此关系最早可被策略感知的时刻


# ═══════════════════════════════════════════
# 便捷构造
# ═══════════════════════════════════════════

def new_event_id(signal_key: str, revision: int) -> str:
    """生成稳定的 event_id: {signal_key}_r{revision}"""
    return f"{signal_key}_r{revision}"


def new_signal_key(
    contract: str,
    timeframe: str,
    bi_idx: int,
    direction: SignalDirection,
    primary_bsp: str,
) -> str:
    """生成稳定的 signal_key: 不含 revision, 同一个信号生命周期内不变"""
    return f"{contract}_{timeframe}_bi{bi_idx}_{direction.value}_{primary_bsp}"


# ═══════════════════════════════════════════
# 研究标签: SignalOutcome
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """信号的历史结果标签 —— 用于 ML 训练和策略研究。

    每个 SignalOutcome 关联到一个 SignalEvent,
    记录该信号在 N 根 K 线后的实际走势结果。

    用法:
        outcome = compute_outcome(event, future_bars, horizon=20, r_unit=100)
        if outcome.hit_2r:
            label = 1  # positive sample for ML
    """

    event_id:          str
    horizon_bars:      int            # 前瞻周期 (K 线根数)

    # ── 收益指标 (以 R 为单位) ──
    forward_return:    float          # horizon 后的 net 收益
    mfe_r:             float          # Maximum Favorable Excursion (最大浮盈 / R)
    mae_r:             float          # Maximum Adverse Excursion (最大浮亏 / R)

    # ── 目标命中 ──
    hit_1r:            bool           # 是否触及 +1R
    hit_2r:            bool           # 是否触及 +2R
    stopped_out:       bool           # 是否先触及 -1R (止损)

    # ── 失效检测 ──
    invalidated_before_target: bool   # 是否在触及止盈前被否定

    # ── 扣除成本后 ──
    net_return_after_cost: float | None  # 扣除手续费+滑点后的 net return

    # ── 上下文 ──
    r_unit:            float          # 初始风险单位 (用于 normalize)
    computed_at:       datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id":                  self.event_id,
            "horizon_bars":              self.horizon_bars,
            "forward_return":            self.forward_return,
            "mfe_r":                     self.mfe_r,
            "mae_r":                     self.mae_r,
            "hit_1r":                    self.hit_1r,
            "hit_2r":                    self.hit_2r,
            "stopped_out":               self.stopped_out,
            "invalidated_before_target": self.invalidated_before_target,
            "net_return_after_cost":     self.net_return_after_cost,
            "r_unit":                    self.r_unit,
            "computed_at":               self.computed_at.isoformat(),
        }


# ═══════════════════════════════════════════
# 人工标注: HumanAnnotation
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class HumanAnnotation:
    """主观交易判断的独立标注 —— 不与自动评分混合。

    用途:
      - 记录「程序评 weak 但人工觉得是买点」的信号
      - 后续对比: 人工接受但程序拒绝的信号, 是否有更高 MFE
    """

    annotation_id:  str
    event_id:       str
    reviewer:       str            # 标注人
    decision:       str            # "accept" / "reject" / "watch"
    confidence:     int            # 1~5
    reason_codes:   tuple[str, ...]
    note:           str            # 自由文本备注
    created_at:     datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation_id":  self.annotation_id,
            "event_id":       self.event_id,
            "reviewer":       self.reviewer,
            "decision":       self.decision,
            "confidence":     self.confidence,
            "reason_codes":   "|".join(self.reason_codes),
            "note":           self.note,
            "created_at":     self.created_at.isoformat(),
        }
