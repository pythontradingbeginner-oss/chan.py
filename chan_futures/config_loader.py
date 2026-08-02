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

from Common.CEnum import BSP_TYPE
from signal_core.models import ScoreGrade
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
from .decision_pipeline import DecisionMode, DecisionPipeline, DecisionPipelineConfig
from .graded_strategy import GradeFilterConfig, GradedChanStrategy
from .multi_level import MultiLevelDecisionEngine, make_multi_level_engine
from .runtime_kernel import RuntimeDecisionKernel
from .sizing import make_sizer


_TYPE_STR_TO_BSP: dict[str, BSP_TYPE] = {
    "1": BSP_TYPE.T1,
    "1p": BSP_TYPE.T1P,
    "2": BSP_TYPE.T2,
    "2s": BSP_TYPE.T2S,
    "3a": BSP_TYPE.T3A,
    "3b": BSP_TYPE.T3B,
}


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


def make_decision_pipeline(config: StrategyConfig) -> DecisionPipeline:
    """Build the single entry-decision kernel shared by every runtime."""
    entry = config.entry
    accepted = _accepted_bsp_values(entry.get("accepted_bsp_types"))
    mode = _decision_mode(entry)
    allow_short = config.allow_short and not bool(entry.get("long_only", False))
    return DecisionPipeline(
        DecisionPipelineConfig(
            policy_id=str(entry.get("policy_id", "chan_entry_v1")),
            mode=mode,
            min_grade=ScoreGrade(config.grading.min_grade.value),
            accepted_bsp_types=accepted,
            allow_short=allow_short,
            require_confirmed=bool(entry.get("require_confirmed_bsp", True)),
            max_abs_position=config.risk.max_abs_position,
        ),
        sizer=make_sizer(
            config.sizing,
            contract_multiplier=config.execution.contract_multiplier,
        ),
    )


def make_graded_strategy(config: StrategyConfig) -> GradedChanStrategy:
    """Build the canonical BSP adapter around the shared decision kernel."""
    entry = config.entry
    accepted_values = _accepted_bsp_values(entry.get("accepted_bsp_types"))
    accepted_types = tuple(_TYPE_STR_TO_BSP[value] for value in accepted_values)
    allow_short = config.allow_short and not bool(entry.get("long_only", False))
    mode = _decision_mode(entry)
    return GradedChanStrategy(
        GradeFilterConfig(
            min_grade=config.grading.min_grade.value,
            accepted_bsp_types=accepted_types,
            allow_short=allow_short,
            require_confirmed_bsp=bool(entry.get("require_confirmed_bsp", True)),
            policy_mode=mode.value,
            policy_id=str(entry.get("policy_id", "chan_entry_v1")),
        ),
        decision_pipeline=make_decision_pipeline(config),
    )


def make_runtime_decision_kernel(
    config: StrategyConfig,
    *,
    symbol: str = "RB",
    timeframe: str | None = None,
    contract: str = "",
    multi_level: MultiLevelDecisionEngine | None = None,
    parent_frame=None,
    child_frame=None,
) -> RuntimeDecisionKernel:
    """Build the canonical decision entry shared by every runtime."""
    from signal_core import SignalExtractor
    from strategy_policy.qingpai_decomposition import QingpaiDecomposer

    resolved_timeframe = timeframe or _timeframe_name(config.kl_type)
    return RuntimeDecisionKernel(
        strategy=make_graded_strategy(config),
        extractor=SignalExtractor(
            symbol=symbol,
            timeframe=resolved_timeframe,
            contract=contract,
        ),
        decomposer=(
            QingpaiDecomposer(level=config.decomposition.level)
            if config.decomposition.enabled
            else None
        ),
        multi_level=(
            multi_level
            if multi_level is not None
            else make_multi_level_engine(
                config,
                parent_frame=parent_frame,
                child_frame=child_frame,
            )
        ),
    )


def _decision_mode(entry: dict[str, Any]) -> DecisionMode:
    raw = entry.get("policy_mode")
    if raw is None:
        raw = (
            DecisionMode.QINGPAI_STRICT.value
            if bool(entry.get("strict_hard_blockers", False))
            else DecisionMode.LEGACY.value
        )
    try:
        return DecisionMode(str(raw))
    except ValueError as exc:
        available = ", ".join(mode.value for mode in DecisionMode)
        raise ValueError(f"未知 entry.policy_mode: {raw}。可用: {available}") from exc


def _accepted_bsp_values(raw: object) -> frozenset[str]:
    if raw is None:
        return frozenset({"1", "1p", "2", "3a", "3b"})
    if isinstance(raw, (str, bytes)):
        raise ValueError("entry.accepted_bsp_types 必须是列表，不能是字符串")
    try:
        values = frozenset(str(value) for value in raw)
    except TypeError as exc:
        raise ValueError("entry.accepted_bsp_types 必须是可迭代列表") from exc
    unknown = values.difference(_TYPE_STR_TO_BSP)
    if unknown:
        raise ValueError(f"未知 BSP 类型: {', '.join(sorted(unknown))}")
    return values


def _timeframe_name(kl_type: str) -> str:
    return kl_type.replace("K_", "").replace("M", "m").lower()


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
