"""统一策略配置 —— 一个 dataclass 驱动整个回测流程。

替代过去散落在 6+ 个脚本中的分散配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


# ═══════════════════════════════════════════
# 嵌套参数 dataclass
# ═══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ChanParams:
    """CChan / CChanConfig 参数（传给 CChan 构造函数）。"""

    bi_strict: bool = True
    divergence_rate: float = float("inf")
    bsp2_follow_1: bool = False
    bsp3_follow_1: bool = False
    min_zs_cnt: int = 0
    bs1_peak: bool = False
    macd_algo: str = "peak"
    bs_type: str = "1,1p,2,2s,3a,3b"
    trigger_step: bool = True
    print_warning: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_step": self.trigger_step,
            "bi_strict": self.bi_strict,
            "divergence_rate": self.divergence_rate,
            "bsp2_follow_1": self.bsp2_follow_1,
            "bsp3_follow_1": self.bsp3_follow_1,
            "min_zs_cnt": self.min_zs_cnt,
            "bs1_peak": self.bs1_peak,
            "macd_algo": self.macd_algo,
            "bs_type": self.bs_type,
            "print_warning": self.print_warning,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChanParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ScoreGradeStr(StrEnum):
    IDEAL = "ideal"
    STANDARD = "standard"
    WEAK = "weak"


@dataclass(frozen=True, slots=True)
class GradingParams:
    """信号评分参数。"""

    min_grade: ScoreGradeStr = ScoreGradeStr.STANDARD

    def __post_init__(self) -> None:
        # 容错：如果传入了普通字符串，自动转换为 ScoreGradeStr
        if isinstance(self.min_grade, str) and not isinstance(self.min_grade, ScoreGradeStr):
            object.__setattr__(self, "min_grade", ScoreGradeStr(self.min_grade))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GradingParams:
        return cls(
            min_grade=ScoreGradeStr(d.get("min_grade", "standard")),
        )


@dataclass(frozen=True, slots=True)
class RiskParams:
    """风控参数。"""

    max_abs_position: int = 1
    max_loss_points: float | None = None
    daily_loss_limit: float | None = None          # 日内累计亏损超限则暂停开仓
    max_consecutive_losses: int | None = None       # 连续亏损笔数超限则暂停
    max_drawdown_pct: float | None = None            # 从权益峰值回撤超限则暂停

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RiskParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class SizingParams:
    """仓位管理参数。"""

    method: Literal["fixed", "atr", "fixed_fractional"] = "fixed"
    lots: int = 1                                    # method=fixed 时的固定手数
    capital: float = 100_000                          # 账户资金（元）
    risk_pct: float = 0.02                            # method=fixed_fractional 时每笔风险比例
    atr_multiplier: float = 2.0                       # method=atr 时的 ATR 倍数
    atr_period: int = 20                              # ATR 计算周期

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SizingParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class ExecutionParams:
    """执行/费用参数。"""

    fee_points: float = 1.0
    slippage_points: float = 1.0
    contract_multiplier: float = 10.0  # RB: 10 元/点/手
    price_tick: float = 1.0
    margin_rate: float = 0.13
    max_margin_utilization: float = 0.50

    def __post_init__(self) -> None:
        if self.fee_points < 0 or self.slippage_points < 0:
            raise ValueError("execution fees and slippage must be non-negative")
        if self.contract_multiplier <= 0 or self.price_tick <= 0:
            raise ValueError("contract_multiplier and price_tick must be positive")
        if not 0 < self.margin_rate < 1:
            raise ValueError("execution.margin_rate must be between 0 and 1")
        if not 0 < self.max_margin_utilization <= 1:
            raise ValueError(
                "execution.max_margin_utilization must be between 0 and 1"
            )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExecutionParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class FilterParams:
    """入场信号过滤参数 —— DC 结构 + OBV + 成交量确认。"""

    enabled: bool = False                              # 是否启用过滤
    dc_threshold_points: float = 30.0                   # DC 方向性变点阈值（点）
    obv_window: int = 40                                # OBV 均线窗口
    volume_window: int = 20                             # 成交量均线窗口
    volume_strength: float = 1.0                        # 成交量放大倍数 (1.0 = 不低于均量)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FilterParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class DecompositionParams:
    """Qingpai strategy-side decomposition settings."""

    enabled: bool = False
    level: Literal["bi", "seg"] = "bi"

    def __post_init__(self) -> None:
        if self.level not in {"bi", "seg"}:
            raise ValueError("decomposition.level must be 'bi' or 'seg'")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DecompositionParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class MultiLevelParams:
    """Point-in-time parent filtering and sub-level confirmation settings."""

    enabled: bool = False
    parent_kl_type: str = "K_60M"
    parent_data_path: str | None = None
    child_kl_type: str = "K_5M"
    child_data_path: str | None = None
    require_parent_direction: bool = True
    require_confirmed_parent: bool = True
    require_child_confirmation: bool = True
    accepted_child_bsp_types: tuple[str, ...] = ("1", "1p", "2")

    def __post_init__(self) -> None:
        if isinstance(self.accepted_child_bsp_types, (str, bytes)):
            raise ValueError(
                "multi_level.accepted_child_bsp_types must be a list, not a string"
            )
        if isinstance(self.accepted_child_bsp_types, list):
            object.__setattr__(
                self,
                "accepted_child_bsp_types",
                tuple(str(value) for value in self.accepted_child_bsp_types),
            )
        if self.enabled:
            if self.parent_kl_type == self.child_kl_type:
                raise ValueError("multi_level parent and child levels must differ")
            if not self.accepted_child_bsp_types:
                raise ValueError("multi_level.accepted_child_bsp_types must not be empty")
            allowed = {"1", "1p", "2", "2s", "3a", "3b"}
            unknown = set(self.accepted_child_bsp_types).difference(allowed)
            if unknown:
                raise ValueError(
                    "unknown multi-level child BSP types: "
                    + ", ".join(sorted(unknown))
                )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MultiLevelParams:
        values = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if "accepted_child_bsp_types" in values:
            if isinstance(values["accepted_child_bsp_types"], (str, bytes)):
                raise ValueError(
                    "multi_level.accepted_child_bsp_types must be a list, not a string"
                )
            values["accepted_child_bsp_types"] = tuple(
                str(value) for value in values["accepted_child_bsp_types"]
            )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class MomentumParams:
    """Qingpai MACD area/peak/price-force confirmation settings."""

    enabled: bool = False
    require_t1_confirmation: bool = True
    require_histogram_confirmation: bool = True
    area_ratio_max: float = 0.90
    area_near_ratio: float = 1.05
    peak_ratio_max: float = 0.95
    price_strength_ratio_max: float = 1.0
    micro_extension_ratio: float = 0.15
    histogram_shrink_ratio: float = 0.90

    def __post_init__(self) -> None:
        positive = {
            "area_ratio_max": self.area_ratio_max,
            "area_near_ratio": self.area_near_ratio,
            "peak_ratio_max": self.peak_ratio_max,
            "price_strength_ratio_max": self.price_strength_ratio_max,
            "micro_extension_ratio": self.micro_extension_ratio,
            "histogram_shrink_ratio": self.histogram_shrink_ratio,
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("momentum ratios must be positive")
        if self.area_ratio_max > self.area_near_ratio:
            raise ValueError("momentum.area_ratio_max must not exceed area_near_ratio")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MomentumParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class ProductionParams:
    """RB production replay rules shared by backtest and paper trading."""

    enabled: bool = False
    adjusted_1m_path: str = "data/processed/RB_1m_continuous_adjusted.parquet"
    use_raw_execution_prices: bool = True
    close_on_rollover: bool = True
    reset_structure_on_rollover: bool = True
    warmup_bars: int = 800

    def __post_init__(self) -> None:
        if self.warmup_bars < 0:
            raise ValueError("production.warmup_bars must be >= 0")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProductionParams:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ═══════════════════════════════════════════
# 出场规则规格
# ═══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ExitRuleSpec:
    """出场规则的序列化规格。

    通过注册表映射到具体的 ExitRule 子类。
    """

    type: str                                        # 类名，如 "TrailingStopRule"
    priority: int = 50
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExitRuleSpec:
        d = dict(d)
        type_ = d.pop("type")
        priority = d.pop("priority", 50)
        return cls(type=type_, priority=priority, params=d)


# ═══════════════════════════════════════════
# 统一 StrategyConfig
# ═══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """驱动一次完整回测的全部配置。

    可从 YAML / JSON 加载，也可在代码中直接构造。
    """

    # ── 品种与数据 ──
    code: str = "RB_MAIN"
    kl_type: str = "K_15M"
    data_path: str = "data/processed/RB_15m_continuous_raw.parquet"
    allow_short: bool = True

    # ── 子配置 ──
    chan: ChanParams = field(default_factory=ChanParams)
    grading: GradingParams = field(default_factory=GradingParams)
    entry: dict[str, Any] = field(default_factory=dict)   # 传给 EntryPolicyConfig
    exits: list[ExitRuleSpec] = field(default_factory=list)
    risk: RiskParams = field(default_factory=RiskParams)
    sizing: SizingParams = field(default_factory=SizingParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    filter: FilterParams = field(default_factory=FilterParams)  # 信号过滤
    decomposition: DecompositionParams = field(default_factory=DecompositionParams)
    multi_level: MultiLevelParams = field(default_factory=MultiLevelParams)
    momentum: MomentumParams = field(default_factory=MomentumParams)
    production: ProductionParams = field(default_factory=ProductionParams)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyConfig:
        return cls(
            code=d.get("code", "RB_MAIN"),
            kl_type=d.get("kl_type", "K_15M"),
            data_path=d.get("data_path", "data/processed/RB_15m_continuous_raw.parquet"),
            allow_short=d.get("allow_short", True),
            chan=ChanParams.from_dict(d.get("chan", {})),
            grading=GradingParams.from_dict(d.get("grading", {})),
            entry=d.get("entry", {}),
            exits=[ExitRuleSpec.from_dict(e) for e in d.get("exits", [])],
            risk=RiskParams.from_dict(d.get("risk", {})),
            sizing=SizingParams.from_dict(d.get("sizing", {})),
            execution=ExecutionParams.from_dict(d.get("execution", {})),
            filter=FilterParams.from_dict(d.get("filter", {})),
            decomposition=DecompositionParams.from_dict(d.get("decomposition", {})),
            multi_level=MultiLevelParams.from_dict(d.get("multi_level", {})),
            momentum=MomentumParams.from_dict(d.get("momentum", {})),
            production=ProductionParams.from_dict(d.get("production", {})),
        )
