"""策略决策模块 —— 入口策略、风险评估、策略组合、出场规则。

核心类:
  EntryPolicy         - 基于规则配置的入口策略
  MultiPolicyEvaluator - 多策略并行评估
  ExitManager / ExitRule - 可插拔出场规则体系
"""

from .entry_policy import EntryPolicy, EntryPolicyConfig, MultiPolicyEvaluator

__all__ = [
    "EntryPolicy",
    "EntryPolicyConfig",
    "MultiPolicyEvaluator",
]

# 子模块不在此处延迟导入 (用户按需 from strategy_policy.exit_rules import ...)
# 保持 strategy_policy 层极轻，不与 signal_core/CChan 耦合
