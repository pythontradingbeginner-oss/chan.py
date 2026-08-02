"""出场规则体系 —— 可插拔、可组合、可审计的出场规则框架。

入口规则 (EntryPolicy) 回答了「什么时候买」，
出场规则 (ExitRule) 回答「什么时候卖」。

与信号管线的关系:
  SignalEvent → SignalAssessment → SignalDecision (入场)
    → Position opened
    → 每根 bar: ExitManager.check(context) → ExitSignal | None
    → 出场触发 → close position

设计原则:
  - 出场规则不可变、无副作用 —— check() 接收 ExitContext，返回 ExitSignal 或 None
  - 完全解耦 —— 不依赖 CBi / CBS_Point / CChan，只消费 ExitContext 的锚点字段
  - 可组合 —— ExitManager 管理多规则优先级，先触发先执行
  - 可审计 —— 每条 ExitSignal 记录 rule_id + reason_code
  - ExitManager 持有可变跟踪状态（trailing peak、bars held），规则本身是纯函数
  - V2: 缠论字段独立为 ChanExitSnapshot，不污染通用 ExitContext
  - V2: ExitAction 支持分级出场 (HOLD / TIGHTEN_STOP / REDUCE / CLOSE_ALL)
"""

from .base import (
    ChanExitSnapshot,
    ExitAction,
    ExitContext,
    ExitDirective,
    ExitManager,
    ExitRule,
    ExitSignal,
    NoExit,
    TrailingState,
)
from .rules import (
    ChanDivergenceExitRule,
    ChanSegmentCompleteExitRule,
    ChanSmallTurnExitRule,
    FixedStopRule,
    MACDCrossRule,
    OppositeSignalRule,
    StructureStopRule,
    StructureInvalidationExitRule,
    TimeStopRule,
    TrailingStopRule,
)

__all__ = [
    # 数据模型
    "ExitSignal",
    "ExitDirective",
    "ExitAction",
    "ExitContext",
    "ChanExitSnapshot",
    "TrailingState",
    "NoExit",
    # 接口
    "ExitRule",
    "ExitManager",
    # 缠论结构出场规则 (chan-native)
    "ChanDivergenceExitRule",
    "ChanSegmentCompleteExitRule",
    "ChanSmallTurnExitRule",
    # 通用规则
    "FixedStopRule",
    "StructureStopRule",
    "StructureInvalidationExitRule",
    "TrailingStopRule",
    "TimeStopRule",
    "OppositeSignalRule",
    "MACDCrossRule",
]
