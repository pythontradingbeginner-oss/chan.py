"""信号规范化核心——将缠论 BSP 转换为可存储、可回测、可审计的扁平化信号记录。

四层管线:
  CChan / CBS_Point  (缠论计算层, 已有)
       │
       ▼
  SignalEvent        (客观事实: 什么时候、看到了什么形态)
       │
       ▼
  SignalAssessment   (评分解释: 某版本评分器对此信号的评价)
       │
       ▼
  SignalDecision     (策略决策: 某策略是否接受此信号)
       │
       ▼
  Fill / Order       (执行结果, 已有 SimulatedExecutionEngine)

核心原则:
  - 除 extractor 模块外，任何模块不得直接依赖 CBi / CKLine_Unit / CChan
  - 所有 dataclass 均为 frozen，构造后不可变
  - signal_key 稳定跨 revision 不变，event_id 每次 revision 新生成
"""

from .models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
    SignalRelation,
    SignalState,
    new_event_id,
    new_signal_key,
)
from .extractor import SignalExtractor
from .lifecycle import (
    LifecycleEntry,
    SignalJournal,
    SignalLifecycleTracker,
)
from .relations import RelationBuilder, check_time_honesty

__all__ = [
    # 模型
    "ScoreGrade",
    "SignalAssessment",
    "SignalDecision",
    "SignalDirection",
    "SignalEvent",
    "SignalRelation",
    "SignalState",
    # 提取
    "SignalExtractor",
    # 生命周期
    "LifecycleEntry",
    "SignalJournal",
    "SignalLifecycleTracker",
    # 关系
    "RelationBuilder",
    "check_time_honesty",
    # 便捷函数
    "new_event_id",
    "new_signal_key",
]
