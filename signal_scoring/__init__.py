"""信号评分模块——每个评分器独立评估 SignalEvent 的结构质量。

评分器独立于策略信号提取:
  - 输入 SignalEvent (纯数据)
  - 输出 SignalAssessment (不可变)
  - 同一个 event 可以被多个评分器并行评估
"""

from .base import SignalScorer, assess_event
from .t1 import T1Scorer
from .t2 import T2Scorer
from .t3 import T3Scorer

__all__ = [
    "SignalScorer",
    "T1Scorer",
    "T2Scorer",
    "T3Scorer",
    "assess_event",
]
