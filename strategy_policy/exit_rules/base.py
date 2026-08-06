"""出场规则基类 —— ExitSignal / ExitContext / ExitRule / ExitManager.

设计原则:
  - 规则是纯函数: ExitRule.check(ExitContext) → ExitSignal | None
  - 所有可变状态由 ExitManager 管理 (TrailingState)
  - ExitContext 是不可变快照, 每根 bar 重新组装
  - 完全解耦——不依赖 CBi / CBS_Point / CChan

V2 新增:
  - ExitAction 枚举 (分级出场动作: HOLD / TIGHTEN_STOP / REDUCE / CLOSE_ALL)
  - ExitDirective (策略指令而非成交结果)
  - ChanExitSnapshot (缠论结构字段的独立容器)
  - ExitRule.category / priority 规则分类排序
  - ExitManager strict 模式 + 异常审计
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext


# ═══════════════════════════════════════════
# ExitAction / ExitDirective — 分级出场
# ═══════════════════════════════════════════

class ExitAction(StrEnum):
    """出场动作类型。

    HOLD          - 不执行任何操作
    TIGHTEN_STOP  - 收紧止损线 (不改变仓位)
    REDUCE        - 减仓 (默认减半, 比例由 reduce_ratio 控制)
    CLOSE_ALL     - 全部平仓
    """
    HOLD         = "hold"
    TIGHTEN_STOP = "tighten_stop"
    REDUCE       = "reduce"
    CLOSE_ALL    = "close_all"


@dataclass(frozen=True, slots=True)
class ExitDirective:
    """单条规则产出的出场指令 (策略判断, 非成交结果)。

    与 ExitSignal 的区别:
      - ExitSignal 始终意味着「全平」
      - ExitDirective 可以是收紧止损、减仓、或全平

    ExitManager.check() 返回 ExitSignal | None (向后兼容),
    但规则内部可从 check_with_directive() 返回 ExitDirective。
    """

    rule_id: str
    reason_code: str
    action: ExitAction = ExitAction.CLOSE_ALL
    trigger_price: float | None = None       # 触发价 (CLOSE_ALL 时的目标成交价)
    target_stop_price: float | None = None   # 新止损价 (TIGHTEN_STOP 时)
    reduce_ratio: float | None = None        # 减仓比例 (REDUCE 时, None 表示减半)
    description: str = ""


# ═══════════════════════════════════════════
# ExitSignal — 不可变的出场决策 (向后兼容)
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ExitSignal:
    """单条规则产出的出场决策 (全平)。

    ExitManager 依次询问规则, 返回第一个非 None 的 ExitSignal。
    """

    rule_id: str
    reason_code: str
    exit_price: float
    description: str = ""

    def __bool__(self) -> bool:
        return True


# 哨兵: 无出场信号
NoExit = None  # type: ExitSignal | None


# ═══════════════════════════════════════════
# ChanExitSnapshot — 缠论结构字段独立容器
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ChanExitSnapshot:
    """缠论结构实时数据的不可变快照。

    从 ExitContext 中独立出来:
      - 通用止损规则只读取 position / bar / risk (无 chan 依赖)
      - 缠论规则读取 ctx.chan.*
      - 非缠论策略不需要伪造这些字段
      - structure_version 用于结构算法版本化

    全部字段可选 — 不传则缠论规则静默跳过。
    """

    # ── 时间 (防未来函数) ──
    available_at: datetime | None = None   # 策略最早可知此结构的时刻

    # ── 线段状态 ──
    segment_complete: bool = False
    segment_direction: str | None = None   # "up" | "down" — 已完成线段的方向
    segment_id: str | None = None          # 线段标识 (用于审计)

    # ── MACD 面积序列 ──
    macd_areas: list[float] | None = None        # 最近几段同向走势的 MACD 面积序列
    macd_area_current: float | None = None       # 最新段面积
    macd_area_previous: float | None = None      # 前一段面积
    macd_area_ratio: float | None = None         # 面积比 = current / previous
    macd_peaks: list[float] | None = None
    price_strengths: list[float] | None = None
    price_slopes: list[float] | None = None
    momentum_status: str | None = None
    momentum_confirmed: bool = False
    momentum_reason_codes: tuple[str, ...] = ()
    histogram_state: str | None = None

    # ── 笔力度 ──
    adjacent_bidong: list[float] | None = None   # 相邻笔的力度 (涨跌点数)
    new_bi_confirmed: bool = False               # 新线段是否已形成至少一笔
    new_bi_amplitude: float | None = None        # 最新一笔振幅

    # ── 极值检测 ──
    is_new_high_low: bool = False                # 本 bar 是否创走势新高 (by high) 或新低 (by low)
    prev_high: float | None = None               # 前一个转折高点
    prev_low: float | None = None                # 前一个转折低点

    # ── 分型确认位 ──
    fractal_break_price: float | None = None     # 最近反向特征序列分型确认位
    fractal_break_confirmed: bool = False        # 分型是否已确认被破坏

    # ── 振幅统计 ──
    bar_amplitude: float | None = None           # 当前 bar 振幅 = abs(high - low)
    avg_amplitude_20: float | None = None        # 最近 20 根 bar 平均振幅

    # ── 版本 ──
    structure_version: str = "v0"


# ═══════════════════════════════════════════
# ExitContext — 每根 bar 的不可变快照
# ═══════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ExitContext:
    """每根 bar 的完整上下文, 由 ExitManager 在 check() 前组装。

    规则只能读取, 不能修改。

    结构:
      - position / bar / risk 字段: 所有规则可用
      - chan: ChanExitSnapshot | None — 仅缠论规则使用
    """

    # ── 当前 bar ──
    bar_end_time: datetime
    open: float
    high: float
    low: float
    close: float

    # ── 持仓信息 ──
    direction: SignalDirection
    entry_price: float

    # ── 入场信号结构锚点 (来自 SignalEvent, 可能为 None) ──
    bi_begin_price: float | None = None
    zs_high: float | None = None
    zs_low: float | None = None

    # ── 入场决策计划 (来自 SignalDecision, 可能为 None) ──
    initial_stop_price: float | None = None
    invalidation_price: float | None = None

    # ── 入场评分 (用于分级出场: WEAK → 更紧的止损) ──
    entry_grade: str | None = None  # "ideal" | "standard" | "weak"

    # ── 持仓期间衍生指标 (ExitManager 计算) ──
    bars_since_entry: int = 0
    current_move: float = 0.0        # sign * (close - entry_price), >0 = 浮盈
    mfe: float = 0.0                 # 最佳浮盈 (close-based, 单调不减)
    mae: float = 0.0                 # 最大浮亏 (close-based, ≤ 0, 单调不增)

    # ── 外部信号 (策略层传入) ──
    opposite_signal_triggered: bool = False

    # ── 技术指标 (策略层可选传入, 供 MACDCrossRule 等使用) ──
    macd_cross_down: bool = False
    macd_cross_up: bool = False

    # ── 缠论结构快照 (非缠论策略传 None) ──
    chan: ChanExitSnapshot | None = None

    # ── 便捷方法 ──

    @property
    def is_long(self) -> bool:
        return self.direction == SignalDirection.LONG

    @property
    def sign(self) -> int:
        return 1 if self.is_long else -1


# ═══════════════════════════════════════════
# TrailingState — ExitManager 内部可变状态
# ═══════════════════════════════════════════

@dataclass
class TrailingState:
    """每笔持仓的内部跟踪状态, 由 ExitManager 管理。

    每次开仓时 reset(), 每根 bar 更新一次。
    """

    bars_since_entry: int = 0
    best_favorable_move: float = 0.0   # close-based MFE (≥ 0)
    worst_adverse_move: float = 0.0    # close-based MAE (≤ 0)

    def reset(self) -> None:
        self.bars_since_entry = 0
        self.best_favorable_move = 0.0
        self.worst_adverse_move = 0.0


# ═══════════════════════════════════════════
# ExitRule — 抽象基类
# ═══════════════════════════════════════════

class ExitRule(ABC):
    """出场规则抽象基类。

    子类只需实现 check(), 接收不可变的 ExitContext, 返回 ExitSignal 或 None。

    规则本身应是无状态的——所有跟踪状态由 ExitManager 管理,
    并通过 ExitContext 的衍生字段 (mfe, mae, bars_since_entry) 暴露。

    V2: 支持通过 check_with_directive() 返回 ExitDirective (分级动作)。
    """

    @property
    def rule_id(self) -> str:
        return self.__class__.__name__

    # ── V2: 规则分类 (供 ExitManager 排序) ──
    # 优先级: 数字越小越先执行
    # 类别顺序: risk > protection > chan_structure > profit > signal > momentum > time > auxiliary
    category: str = "protection"
    priority: int = 50

    @abstractmethod
    def check(self, ctx: ExitContext) -> ExitSignal | None:
        """检查是否触发出场。"""
        ...

    # ── V2: 分级出场接口 ──
    def check_with_directive(self, ctx: ExitContext) -> ExitDirective:
        """返回分级指令 (默认: 如果 check() 触发则全平, 否则 HOLD)。

        子类可重写以返回 TIGHTEN_STOP / REDUCE 等非全平动作。
        """
        signal = self.check(ctx)
        if signal is not None:
            return ExitDirective(
                rule_id=self.rule_id,
                reason_code=signal.reason_code,
                action=ExitAction.CLOSE_ALL,
                trigger_price=signal.exit_price,
                description=signal.description,
            )
        return ExitDirective(
            rule_id=self.rule_id,
            reason_code="none",
            action=ExitAction.HOLD,
        )

    # ── 方向工具 (子类共享) ──

    @staticmethod
    def _stop_hit(
        direction: SignalDirection,
        low: float,
        high: float,
        stop_price: float,
    ) -> bool:
        """当前 bar 是否触及止损价。"""
        if direction == SignalDirection.LONG:
            return low <= stop_price
        return high >= stop_price


# ═══════════════════════════════════════════
# ExitManager — 多规则调度 + 状态管理
# ═══════════════════════════════════════════

class ExitManager:
    """管理多条出场规则和每笔持仓的跟踪状态。

    核心流程:
      1. 开仓 → on_position_opened()  重置状态 + 安装统一持仓上下文
      2. 每 bar → check()   更新跟踪 → 组装 ExitContext → 依次询问规则
      3. 平仓 → on_close()  清理状态 (或下次 on_entry 自动覆盖)
    """

    def __init__(
        self,
        rules: list[ExitRule] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        self._rules: list[ExitRule] = sorted(
            list(rules) if rules else [],
            key=_rule_sort_key,
        )
        self._strict = strict
        self._rule_error_counts: dict[str, int] = {}
        self._tracking = TrailingState()

        self._position: PositionContext | None = None

    # ── 规则管理 ──

    @property
    def rules(self) -> list[ExitRule]:
        return list(self._rules)

    @property
    def rule_ids(self) -> list[str]:
        return [r.rule_id for r in self._rules]

    def add_rule(self, rule: ExitRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=_rule_sort_key)

    def remove_rule(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        return len(self._rules) < before

    # ── V2: strict 模式 + 异常追踪 ──

    @property
    def strict(self) -> bool:
        return self._strict

    def error_summary(self) -> dict[str, int]:
        """返回各条规则的异常次数。回测结束时调用，用于审计。"""
        return dict(self._rule_error_counts)

    def error_summary_text(self) -> str:
        """返回可读的异常汇总。"""
        if not self._rule_error_counts:
            return "ExitManager: 无规则异常。"
        lines = ["ExitManager 规则异常汇总:"]
        for rid, count in sorted(self._rule_error_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {rid}: {count} 次异常")
        return "\n".join(lines)

    # ── 状态属性 (只读) ──

    @property
    def is_active(self) -> bool:
        return self._position is not None

    @property
    def entry_price(self) -> float:
        return self._position.entry_price if self._position is not None else 0.0

    @property
    def position_context(self) -> PositionContext | None:
        return self._position

    @property
    def bars_since_entry(self) -> int:
        return self._tracking.bars_since_entry

    @property
    def mfe(self) -> float:
        return self._tracking.best_favorable_move

    # ── 核心接口 ──

    def on_position_opened(self, context: PositionContext) -> None:
        """Install the canonical fill-derived holding context."""
        self._tracking.reset()
        self._position = context

    def on_position_updated(self, context: PositionContext) -> None:
        """Replace fill/volume state without resetting holding-period trackers."""
        if self._position is None:
            raise RuntimeError("cannot update an inactive position")
        if self._position.direction != context.direction:
            raise ValueError("position direction cannot change during an update")
        self._position = context

    def on_entry(
        self,
        *,
        direction: SignalDirection,
        entry_price: float,
        bi_begin_price: float | None = None,
        zs_high: float | None = None,
        zs_low: float | None = None,
        initial_stop_price: float | None = None,
        invalidation_price: float | None = None,
        entry_grade: str | None = None,
    ) -> None:
        """开仓时调用——重置跟踪状态并记录入口锚点。

        所有锚点字段都是可选的——如果策略没有对应的信号管线,
        直接传 None 即可, 依赖这些锚点的规则会自行跳过。
        """
        self.on_position_opened(
            PositionContext.from_legacy_entry(
                direction=direction,
                entry_price=entry_price,
                bi_begin_price=bi_begin_price,
                zs_high=zs_high,
                zs_low=zs_low,
                initial_stop_price=initial_stop_price,
                invalidation_price=invalidation_price,
                entry_grade=entry_grade,
            )
        )

    def on_close(self) -> None:
        """平仓时调用——清理跟踪状态。"""
        self._tracking.reset()
        self._position = None

    def snapshot_state(self) -> dict:
        """返回可恢复的跟踪状态（供带仓重启持久化）。

        P7-R3 公共接口：策略通过该接口读取 ExitManager 跟踪状态，
        不得直接拼接私有字段。包含 bars_since_entry / MFE / MAE。
        """
        return {
            "bars_since_entry": self._tracking.bars_since_entry,
            "mfe": self._tracking.best_favorable_move,
            "mae": self._tracking.worst_adverse_move,
        }

    def restore_state(self, payload: dict) -> None:
        """从快照恢复跟踪状态。

        P7-R3 公共接口：带仓重启后，在 on_position_opened() 之后调用，
        恢复 bars_since_entry / MFE / MAE，使退出跟踪与重启前一致。
        空快照或非法字段忽略（保持 fail-closed 语义）。
        """
        if not isinstance(payload, dict):
            return
        try:
            bars = int(payload.get("bars_since_entry", 0))
            mfe = float(payload.get("mfe", 0.0))
            mae = float(payload.get("mae", 0.0))
        except (TypeError, ValueError):
            return
        self._tracking.bars_since_entry = max(0, bars)
        self._tracking.best_favorable_move = max(0.0, mfe)
        self._tracking.worst_adverse_move = min(0.0, mae)

    def check(
        self,
        *,
        bar_end_time: datetime,
        open: float,
        high: float,
        low: float,
        close: float,
        opposite_signal_triggered: bool = False,
        macd_cross_down: bool = False,
        macd_cross_up: bool = False,
        # ── 缠论结构快照 (策略层传入, 非缠论策略不传) ──
        chan_snapshot: ChanExitSnapshot | None = None,
    ) -> ExitSignal | None:
        """每根 bar 调用——更新跟踪状态, 组装上下文, 依次询问规则。

        Returns:
            第一个触发的 ExitSignal, 或 None (没有规则触发)。
        """
        position = self._position
        if position is None:
            return None

        direction = position.direction
        sign = 1 if direction == SignalDirection.LONG else -1

        # ── 更新跟踪 ──
        self._tracking.bars_since_entry += 1
        current_move = sign * (float(close) - position.entry_price)
        if current_move > self._tracking.best_favorable_move:
            self._tracking.best_favorable_move = current_move
        if current_move < self._tracking.worst_adverse_move:
            self._tracking.worst_adverse_move = current_move

        # ── 组装 ExitContext ──
        ctx = ExitContext(
            bar_end_time=bar_end_time,
            open=float(open),
            high=float(high),
            low=float(low),
            close=float(close),
            direction=direction,
            entry_price=position.entry_price,
            bi_begin_price=position.bi_begin_price,
            zs_high=position.zs_high,
            zs_low=position.zs_low,
            initial_stop_price=position.execution_stop_price,
            invalidation_price=position.setup_invalidation_price,
            entry_grade=position.entry_grade,
            bars_since_entry=self._tracking.bars_since_entry,
            current_move=current_move,
            mfe=self._tracking.best_favorable_move,
            mae=self._tracking.worst_adverse_move,
            opposite_signal_triggered=opposite_signal_triggered,
            macd_cross_down=macd_cross_down,
            macd_cross_up=macd_cross_up,
            chan=chan_snapshot,
        )

        # ── 依次询问规则 (先触发的胜出) ──
        for rule in self._rules:
            try:
                result = rule.check(ctx)
                if result is not None:
                    return result
            except Exception:
                self._rule_error_counts[rule.rule_id] = \
                    self._rule_error_counts.get(rule.rule_id, 0) + 1
                if self._strict:
                    raise
                continue

        return None


# ── 规则排序辅助 (module-level) ──────────────────────────

# category 到排序序号的映射 (数字越小越先执行)
_CATEGORY_ORDER: dict[str, int] = {
    "risk": 1,
    "protection": 10,
    "chan_structure": 15,
    "profit": 30,
    "signal": 40,
    "momentum": 45,
    "time": 50,
    "auxiliary": 60,
}


def _rule_sort_key(rule: ExitRule) -> tuple[int, int, str]:
    """规则排序键: (category_order, priority_within_category, rule_id)。

    这确保了相同类别的规则按 priority 排序,
    不同类别的按类别固有顺序排序。
    """
    cat_order = _CATEGORY_ORDER.get(rule.category, 99)
    return (cat_order, rule.priority, rule.rule_id)
