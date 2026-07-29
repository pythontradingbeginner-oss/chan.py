"""配置加载器 —— YAML/JSON → StrategyConfig + 出场规则注册表。

用法:
    from chan_futures.config_loader import load_config

    config = load_config("configs/rb_15m_trend_ideal.yaml")
    backtest = run_backtest(config)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strategy_policy.exit_rules import (
    ChanDivergenceExitRule,
    ChanSegmentCompleteExitRule,
    ChanSmallTurnExitRule,
    ExitManager,
    ExitRule,
    FixedStopRule,
    MACDCrossRule,
    OppositeSignalRule,
    StructureStopRule,
    TimeStopRule,
    TrailingStopRule,
)

from .config import ExitRuleSpec, StrategyConfig


# ═══════════════════════════════════════════
# 出场规则注册表
# ═══════════════════════════════════════════

_RULE_REGISTRY: dict[str, type[ExitRule]] = {
    "StructureStopRule": StructureStopRule,
    "FixedStopRule": FixedStopRule,
    "TrailingStopRule": TrailingStopRule,
    "TimeStopRule": TimeStopRule,
    "OppositeSignalRule": OppositeSignalRule,
    "MACDCrossRule": MACDCrossRule,
    "ChanDivergenceExitRule": ChanDivergenceExitRule,
    "ChanSegmentCompleteExitRule": ChanSegmentCompleteExitRule,
    "ChanSmallTurnExitRule": ChanSmallTurnExitRule,
}



def register_exit_rule(name: str, rule_cls: type[ExitRule]) -> None:
    """向注册表添加自定义出场规则。应在 load_config() 之前调用。"""
    if name in _RULE_REGISTRY:
        raise ValueError(f"规则 {name} 已注册")
    _RULE_REGISTRY[name] = rule_cls


def _build_exit_rules(specs: list[ExitRuleSpec]) -> list[ExitRule]:
    """从规格列表构建出场规则实例。"""
    rules: list[ExitRule] = []
    for spec in specs:
        rule_cls = _RULE_REGISTRY.get(spec.type)
        if rule_cls is None:
            available = ", ".join(sorted(_RULE_REGISTRY))
            raise ValueError(
                f"未知出场规则类型: {spec.type}。可用: {available}"
            )
        try:
            rule = rule_cls(**spec.params)
            rule.priority = spec.priority
            rules.append(rule)
        except TypeError as e:
            raise ValueError(
                f"构建 {spec.type} 失败，参数: {spec.params}，错误: {e}"
            ) from e
    return rules


def _build_exit_manager(specs: list[ExitRuleSpec]) -> ExitManager:
    """从规格列表构建 ExitManager。"""
    rules = _build_exit_rules(specs)
    return ExitManager(rules)


# ═══════════════════════════════════════════
# 主加载函数
# ═══════════════════════════════════════════


def load_config(path: str | Path) -> StrategyConfig:
    """从 YAML 或 JSON 文件加载策略配置。

    支持 .yaml / .yml / .json 扩展名。
    """
    path = Path(path)
    raw = _read_config_file(path)
    return StrategyConfig.from_dict(raw)


def load_config_from_dict(raw: dict[str, Any]) -> StrategyConfig:
    """从字典构建策略配置（无需文件）。用于编程式构造。"""
    return StrategyConfig.from_dict(raw)


def make_exit_manager(config: StrategyConfig) -> ExitManager:
    """从配置构建 ExitManager。

    便捷方法：等价于 _build_exit_manager(config.exits)。
    """
    return _build_exit_manager(config.exits)


# ═══════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════


def _read_config_file(path: Path) -> dict[str, Any]:
    """读取并解析 YAML 或 JSON 配置文件。"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8")

    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "需要 PyYAML 才能加载 YAML 配置文件。请运行: pip install pyyaml"
            ) from None
        data = yaml.safe_load(raw_text)
    elif suffix == ".json":
        data = json.loads(raw_text)
    else:
        raise ValueError(
            f"不支持的配置文件格式: {suffix}。请使用 .yaml / .yml / .json"
        )

    if not isinstance(data, dict):
        raise ValueError("配置文件顶级必须是字典")

    return data
