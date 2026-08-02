"""出场规则具体实现。

每条规则都是 ExitRule 的子类, 无内部可变状态,
只通过 ExitContext 读取当前 bar 快照。

规则列表:
  - StructureStopRule   结构锚点止损 (笔起始价 / 中枢边界)
  - FixedStopRule       固定点数止损 (从 entry_price 算起)
  - TrailingStopRule    跟踪止损 (触发后按回撤比例)
  - TimeStopRule        持仓时间上限
  - OppositeSignalRule  反向信号出场
  - MACDCrossRule       MACD 死叉/金叉出场
"""

from __future__ import annotations

from .base import (
    ChanExitSnapshot,
    ExitAction,
    ExitContext,
    ExitDirective,
    ExitRule,
    ExitSignal,
)
from signal_core.models import SignalDirection


# ═══════════════════════════════════════════
# StructureStopRule — 结构锚点止损
# ═══════════════════════════════════════════

class StructureStopRule(ExitRule):
    """基于入场信号结构锚点的止损。

    P2 后优先执行 ctx.initial_stop_price（订单保护止损）。
    ctx.invalidation_price 是整套分析的失效位，不参与该订单止损择优。
    旧调用方没有 initial_stop_price 时，才按旧语义回退到
    invalidation_price/笔起点/中枢边界。

    逻辑:
      LONG:  跌破上述价位 → 出场
      SHORT: 涨破上述价位 → 出场

    参数:
      grade_tighten: True 时, WEAK 信号的止损会向 entry_price 收紧 50%
        (止损幅度减半, 即亏损限制更紧)
    """

    def __init__(self, grade_tighten: bool = True) -> None:
        self.grade_tighten = grade_tighten

    @property
    def rule_id(self) -> str:
        return "StructureStopRule"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        stop_price = self._resolve_stop(ctx)
        if stop_price is None:
            return None

        triggered = self._stop_hit(ctx.direction, ctx.low, ctx.high, stop_price)
        if not triggered:
            return None

        exit_price = self._executable_stop_price(ctx, stop_price)
        gap = exit_price != stop_price
        return ExitSignal(
            rule_id=self.rule_id,
            reason_code="structure_stop",
            exit_price=exit_price,
            description=(
                f"结构止损跳空触发 @ {exit_price:.1f} (计划 {stop_price:.1f})"
                if gap
                else f"结构止损触发 @ {stop_price:.1f}"
            ),
        )

    def _resolve_stop(self, ctx: ExitContext) -> float | None:
        """按优先级解析有效的止损价位。"""
        is_long = ctx.is_long

        if ctx.initial_stop_price is not None:
            explicit = float(ctx.initial_stop_price)
            if (is_long and explicit < ctx.entry_price) or (
                not is_long and explicit > ctx.entry_price
            ):
                return explicit

        candidates: list[float] = []

        # 旧入口没有订单止损计划时的兼容回退。
        if ctx.invalidation_price is not None:
            candidates.append(float(ctx.invalidation_price))
        if ctx.bi_begin_price is not None:
            candidates.append(float(ctx.bi_begin_price))

        if is_long and ctx.zs_low is not None:
            candidates.append(float(ctx.zs_low))
        elif not is_long and ctx.zs_high is not None:
            candidates.append(float(ctx.zs_high))

        if not candidates:
            return None

        # LONG: 取最紧的止损 (价格最高, 离 entry 最近)
        # SHORT: 取最紧的止损 (价格最低, 离 entry 最近)
        if is_long:
            stop = max(candidates)
        else:
            stop = min(candidates)

        # Grade tightening: WEAK 信号 → 收紧止损
        if self.grade_tighten and ctx.entry_grade == "weak":
            stop = self._tighten(ctx.entry_price, stop, is_long)

        return stop

    @staticmethod
    def _executable_stop_price(ctx: ExitContext, stop_price: float) -> float:
        """跳空穿越保护位时，只能按该 bar 首个可成交价执行。"""
        if ctx.is_long and ctx.open < stop_price:
            return float(ctx.open)
        if not ctx.is_long and ctx.open > stop_price:
            return float(ctx.open)
        return stop_price

    @staticmethod
    def _tighten(entry_price: float, raw_stop: float, is_long: bool) -> float:
        """将止损收紧到 entry 和 raw_stop 的中点。"""
        if is_long:
            if raw_stop >= entry_price:
                return raw_stop
            return entry_price - abs(entry_price - raw_stop) * 0.5
        else:
            if raw_stop <= entry_price:
                return raw_stop
            return entry_price + abs(entry_price - raw_stop) * 0.5


# ═══════════════════════════════════════════
# FixedStopRule — 固定点数止损
# ═══════════════════════════════════════════

class FixedStopRule(ExitRule):
    """固定点数/百分比止损。

    参数:
      stop_points: 止损点数 (>0), 从 entry_price 算起。
        例如 stop_points=180, entry=3800 → LONG 止损=3620, SHORT 止损=3980
      grade_adjust: True 时按 grade 调整点数:
        IDEAL → stop_points, STANDARD → 0.8×, WEAK → 0.5×

    注意: 点数单位与品种单位一致。RB 为例, 1 point = 1 元/吨, 180 点 ≈ 0.47%。
    """

    def __init__(
        self,
        stop_points: float = 180.0,
        *,
        grade_adjust: bool = True,
    ) -> None:
        if stop_points <= 0:
            raise ValueError("stop_points must be positive")
        self.stop_points = float(stop_points)
        self.grade_adjust = grade_adjust

    @property
    def rule_id(self) -> str:
        return f"FixedStopRule_{int(self.stop_points)}pts"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        effective_points = self._effective_points(ctx)
        stop_price = (
            ctx.entry_price - effective_points if ctx.is_long
            else ctx.entry_price + effective_points
        )

        triggered = self._stop_hit(ctx.direction, ctx.low, ctx.high, stop_price)
        if not triggered:
            return None

        return ExitSignal(
            rule_id=self.rule_id,
            reason_code="fixed_stop",
            exit_price=stop_price,
            description=f"固定止损 {effective_points:.0f}pts 触发 @ {stop_price:.1f}",
        )

    def _effective_points(self, ctx: ExitContext) -> float:
        if not self.grade_adjust or ctx.entry_grade is None:
            return self.stop_points
        mult = {"ideal": 1.0, "standard": 0.8, "weak": 0.5}.get(ctx.entry_grade, 1.0)
        return self.stop_points * mult


# ═══════════════════════════════════════════
# TrailingStopRule — 跟踪止损
# ═══════════════════════════════════════════

class TrailingStopRule(ExitRule):
    """基于 MFE 的跟踪止损。

    工作原理:
      1. 跟踪 close-based MFE (ExitContext.mfe, 由 ExitManager 更新)
      2. 当 MFE ≥ trigger_points 时, 激活 trailing stop
      3. 一旦浮盈回撤超过 mfe × giveback_ratio, 即触发出场

    参数:
      trigger_points: MFE 达到多少才激活跟踪 (>0)
      giveback_ratio: 允许回撤比例 (0~1)
        例如 trigger=500, giveback=0.5:
          MFE 达到 500pts 后, 回撤超过 250pts (=500×0.5) 触发出场

    注意: 此规则只检查 close 价格浮盈的 tracker。
    不含高低点极值 tracker — 那种「真正最高点」的跟踪需要扩展 TrailingState
    (但 close-based 在 bar-level 回测中已经足够, 并且避免了未来函数风险)。
    """

    def __init__(
        self,
        trigger_points: float = 500.0,
        giveback_ratio: float = 0.5,
    ) -> None:
        if trigger_points <= 0:
            raise ValueError("trigger_points must be positive")
        if not 0 < giveback_ratio < 1:
            raise ValueError("giveback_ratio must be between 0 and 1")
        self.trigger_points = float(trigger_points)
        self.giveback_ratio = float(giveback_ratio)

    @property
    def rule_id(self) -> str:
        return f"TrailingStopRule_{int(self.trigger_points)}_{int(self.giveback_ratio * 100)}"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        # 未达到触发线
        if ctx.mfe < self.trigger_points:
            return None

        # 当前回撤
        giveback = ctx.mfe - ctx.current_move
        if giveback < ctx.mfe * self.giveback_ratio:
            return None

        return ExitSignal(
            rule_id=self.rule_id,
            reason_code="trailing_stop",
            exit_price=ctx.close,
            description=(
                f"跟踪止损: MFE={ctx.mfe:.0f}, "
                f"回撤={giveback:.0f} ≥ {ctx.mfe * self.giveback_ratio:.0f}"
            ),
        )


# ═══════════════════════════════════════════
# TimeStopRule — 持仓时间上限
# ═══════════════════════════════════════════

class TimeStopRule(ExitRule):
    """持仓 K 线根数上限。

    参数:
      max_bars: 允许的最大持仓 bar 数 (不含入场 bar 本身)
        例如 max_bars=192, 15m 周期 ≈ 48 小时 = 12 个交易 session
    """

    def __init__(self, max_bars: int = 192) -> None:
        if max_bars <= 0:
            raise ValueError("max_bars must be positive")
        self.max_bars = max_bars

    @property
    def rule_id(self) -> str:
        return f"TimeStopRule_{self.max_bars}b"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        if ctx.bars_since_entry < self.max_bars:
            return None

        return ExitSignal(
            rule_id=self.rule_id,
            reason_code="time_stop",
            exit_price=ctx.close,
            description=f"持仓已 {ctx.bars_since_entry} bar ≥ {self.max_bars}",
        )


# ═══════════════════════════════════════════
# OppositeSignalRule — 反向信号出场
# ═══════════════════════════════════════════

class OppositeSignalRule(ExitRule):
    """检测到反向缠论信号时出场。

    调用方需要在策略层维护 opposite_signal_triggered 标志,
    并在 check() 时传入。

    典型用法:
      - 多仓 + 出现卖点 (二卖/三卖) → 出场
      - 空仓 + 出现买点 (二买/三买) → 出场

    参数:
      mode: "flat" = 平仓; "reverse" = 平仓并反手 (由策略层处理)
        此处 always 只是信号检测, 实际反手逻辑在策略层。
      only_high_grade: True 时仅 IDEAL 级别的反向信号才算
    """

    def __init__(self, mode: str = "flat", *, only_high_grade: bool = False) -> None:
        if mode not in ("flat", "reverse"):
            raise ValueError("mode must be 'flat' or 'reverse'")
        self.mode = mode
        self.only_high_grade = only_high_grade

    @property
    def rule_id(self) -> str:
        grade_suffix = "_high" if self.only_high_grade else ""
        return f"OppositeSignalRule_{self.mode}{grade_suffix}"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        if not ctx.opposite_signal_triggered:
            return None

        reason = "opposite_signal_flat" if self.mode == "flat" else "opposite_signal_reverse"
        return ExitSignal(
            rule_id=self.rule_id,
            reason_code=reason,
            exit_price=ctx.close,
            description=f"反向信号触发, 模式={self.mode}",
        )


# ═══════════════════════════════════════════
# MACDCrossRule — MACD 死叉/金叉出场
# ═══════════════════════════════════════════

class MACDCrossRule(ExitRule):
    """MACD DIF 下穿/上穿 DEA 时出场。

    交叉检测由策略层完成并传入 macd_cross_down / macd_cross_up 布尔值。
    规则只消费布尔事件——这避免了「开仓即出场」的 bug。

    策略层用法:
        prev_dif, prev_dea = ...
        curr_dif, curr_dea = macd()
        cross_down = prev_dif > prev_dea and curr_dif <= curr_dea
        exit_signal = manager.check(..., macd_cross_down=cross_down)
    """

    category = "auxiliary"
    priority = 60

    @property
    def rule_id(self) -> str:
        return "MACDCrossRule"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        if ctx.is_long and ctx.macd_cross_down:
            return ExitSignal(
                rule_id=self.rule_id,
                reason_code="macd_death_cross",
                exit_price=ctx.close,
                description="MACD 死叉出场",
            )
        if not ctx.is_long and ctx.macd_cross_up:
            return ExitSignal(
                rule_id=self.rule_id,
                reason_code="macd_golden_cross",
                exit_price=ctx.close,
                description="MACD 金叉出场",
            )

        return None


# ═══════════════════════════════════════════
# 缠论结构出场规则 (chan-native exit rules)
# ═══════════════════════════════════════════
# 以下三条规则直接体现缠论的核心出场逻辑:
#   ChanDivergenceExitRule       — 走势力度背驰 = 主动止盈
#   ChanSegmentCompleteExitRule  — 线段终结 = 走势完成
#   ChanSmallTurnExitRule        — 小转大 = 突发结构破坏
#
# 它们都属于 category="chan_structure"（优先级 15，介于 protection 与 profit 之间）。
# 缠论主动离场应先于被动止损，后于强制风控。

# ═══════════════════════════════════════════
# ChanDivergenceExitRule — 走势力度背驰出场
# ═══════════════════════════════════════════

class ChanDivergenceExitRule(ExitRule):
    """基于缠论「走势力度背驰」的主动出场。

    缠论原理:
      趋势走势的动力衰竭通过相邻同向段的 MACD 面积对比来衡量。
      若后一段的面积小于前一段，则为「背驰」(divergence)。

    V2: 默认动作为 TIGHTEN_STOP（收紧止损），而非 CLOSE_ALL。
      首次背驰更适合作为预警——只有次级别确认+破位后才升级为全平。
      重写 check_with_directive() 实现分级出场。

    参数:
      require_second_level_confirm: 是否需要次级别确认（区间套）。
      divergence_type: "trend" | "disk" | "both"。
      min_bars_for_compare: 每段走势至少包含多少根 bar。
    """

    category = "chan_structure"
    priority = 10

    def __init__(
        self,
        *,
        require_second_level_confirm: bool = True,
        divergence_type: str = "both",
        min_bars_for_compare: int = 5,
    ) -> None:
        if divergence_type not in ("trend", "disk", "both"):
            raise ValueError("divergence_type must be 'trend', 'disk', or 'both'")
        self.require_second_level_confirm = require_second_level_confirm
        self.divergence_type = divergence_type
        self.min_bars_for_compare = min_bars_for_compare

    @property
    def rule_id(self) -> str:
        confirm = "_2nd" if self.require_second_level_confirm else ""
        return f"ChanDivergenceExitRule{confirm}"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        # check() 只返回全平信号; TIGHTEN_STOP/REDUCE 由 check_with_directive() 处理
        directive = self.check_with_directive(ctx)
        if directive.action != ExitAction.CLOSE_ALL:
            return None
        return ExitSignal(
            rule_id=self.rule_id,
            reason_code=directive.reason_code,
            exit_price=directive.trigger_price or ctx.close,
            description=directive.description,
        )

    def check_with_directive(self, ctx: ExitContext) -> ExitDirective:
        """背驰分级出场: 先收紧止损, 次级别确认后再全平。"""
        chan = ctx.chan
        if chan is None:
            return _hold(self)

        if chan.macd_areas is None or len(chan.macd_areas) < 2:
            return _hold(self)

        if not chan.is_new_high_low:
            return _hold(self)

        is_divergent = self._check_divergence(chan, ctx.is_long)
        if not is_divergent:
            return _hold(self)

        # 次级别确认
        sub_confirmed = True
        if self.require_second_level_confirm:
            sub_confirmed = self._second_level_confirm(chan, ctx.is_long)

        if sub_confirmed:
            # 次级别也背驰 → 全平
            return ExitDirective(
                rule_id=self.rule_id,
                reason_code="chan_divergence",
                action=ExitAction.CLOSE_ALL,
                trigger_price=ctx.close,
                description=self._build_description(chan),
            )
        else:
            # 主级别背驰但次级别未确认 → 收紧止损到最近中枢或结构锚点
            new_stop = self._calc_tightened_stop(ctx)
            return ExitDirective(
                rule_id=self.rule_id,
                reason_code="chan_divergence_warning",
                action=ExitAction.TIGHTEN_STOP if new_stop is not None else ExitAction.HOLD,
                target_stop_price=new_stop,
                trigger_price=ctx.close,
                description=f"背驰预警: 收紧止损 @ {new_stop:.0f}" if new_stop else "背驰预警",
            )

    def _check_divergence(self, chan: ChanExitSnapshot, is_long: bool) -> bool:
        areas: list[float] = chan.macd_areas  # type: ignore[assignment]
        if len(areas) < 2:
            return False

        if is_long:
            same_dir = [a for a in areas if a > 0]
        else:
            same_dir = [a for a in areas if a < 0]

        if len(same_dir) < 2:
            return False

        prev = same_dir[-2]
        curr = same_dir[-1]
        if is_long:
            return curr < prev
        return abs(curr) < abs(prev)

    def _second_level_confirm(self, chan: ChanExitSnapshot, is_long: bool) -> bool:
        bd = chan.adjacent_bidong
        if bd is None or len(bd) < 2:
            return True  # 无数据不阻止
        if is_long:
            same_dir = [b for b in bd if b > 0]
        else:
            same_dir = [b for b in bd if b < 0]
        if len(same_dir) < 2:
            return True
        prev = same_dir[-2]
        curr = same_dir[-1]
        if is_long:
            return curr < prev
        return abs(curr) < abs(prev)

    @staticmethod
    def _calc_tightened_stop(ctx: ExitContext) -> float | None:
        """计算收紧后的止损价。

        多仓: 止损移到「最近中枢下沿」或「entry+0.5×浮盈」中的较高者
        空仓: 止损移到「最近中枢上沿」或「entry-0.5×浮盈」中的较低者
        """
        candidates: list[float] = []
        if ctx.is_long and ctx.zs_low is not None:
            candidates.append(float(ctx.zs_low))
        if not ctx.is_long and ctx.zs_high is not None:
            candidates.append(float(ctx.zs_high))
        if ctx.current_move > 0:
            half_move = ctx.entry_price + 0.5 * ctx.current_move
            candidates.append(half_move)
        if not candidates:
            return None
        return max(candidates) if ctx.is_long else min(candidates)

    def _build_description(self, chan: ChanExitSnapshot) -> str:
        areas = chan.macd_areas
        if areas and len(areas) >= 2:
            return f"缠论背驰出场: area={areas[-2]:.0f}→{areas[-1]:.0f}"
        return "缠论背驰出场"


# ═══════════════════════════════════════════
# ChanSegmentCompleteExitRule — 线段终结出场
# ═══════════════════════════════════════════

class ChanSegmentCompleteExitRule(ExitRule):
    """基于「线段终结」的出场——「走势终完美」的实现。

    缠论原理:
      任何级别的走势终将完美（完成）。当一条线段的终点被确认，
      意味着该方向的结构推动力已耗尽。

    V2: 默认动作为 REDUCE（减仓），而非 CLOSE_ALL。
      线段终结不必然等于趋势反转——可能只是进入次级别回调。
      只有新线段已形成反向笔且破位后才升级为全平。

    参数:
      require_new_bi: True 时要求新线段至少形成一笔才减仓。
      min_new_bi_amplitude: 新笔最小振幅（点数），用于过滤弱反弹。
    """

    category = "chan_structure"
    priority = 15

    def __init__(
        self,
        *,
        require_new_bi: bool = True,
        min_new_bi_amplitude: float | None = None,
    ) -> None:
        self.require_new_bi = require_new_bi
        self.min_new_bi_amplitude = min_new_bi_amplitude

    @property
    def rule_id(self) -> str:
        parts = ["ChanSegmentCompleteExitRule"]
        if self.require_new_bi:
            parts.append("newbi")
        if self.min_new_bi_amplitude is not None:
            parts.append(f"amp{int(self.min_new_bi_amplitude)}")
        return "_".join(parts)

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        directive = self.check_with_directive(ctx)
        if directive.action != ExitAction.CLOSE_ALL:
            return None
        return ExitSignal(
            rule_id=self.rule_id,
            reason_code=directive.reason_code,
            exit_price=directive.trigger_price or ctx.close,
            description=directive.description,
        )

    def check_with_directive(self, ctx: ExitContext) -> ExitDirective:
        """线段终结分级出场: 先减仓, 结构确认破位后全平。"""
        chan = ctx.chan
        if chan is None:
            return _hold(self)
        if not chan.segment_complete:
            return _hold(self)

        new_bi_ok = self._new_bi_exists(chan, ctx.is_long)
        direction_opposes = self._new_segment_opposes(chan, ctx)

        if new_bi_ok and direction_opposes:
            # 线段终结 + 反向笔形成 + 方向相反 → 全平
            return ExitDirective(
                rule_id=self.rule_id,
                reason_code="chan_segment_complete",
                action=ExitAction.CLOSE_ALL,
                trigger_price=ctx.close,
                description="缠论线段终结出场",
            )
        elif chan.segment_complete:
            # 线段终结但尚未确认反向推动 → 减仓
            return ExitDirective(
                rule_id=self.rule_id,
                reason_code="chan_segment_warning",
                action=ExitAction.REDUCE,
                trigger_price=ctx.close,
                description="线段终结预警: 减仓",
            )

        return _hold(self)

    def _new_bi_exists(self, chan: ChanExitSnapshot, is_long: bool) -> bool:
        if not self.require_new_bi:
            return True
        if chan.adjacent_bidong is None or len(chan.adjacent_bidong) == 0:
            return True
        latest_bi = chan.adjacent_bidong[-1]
        opposite = (is_long and latest_bi < 0) or (not is_long and latest_bi > 0)
        if not opposite:
            return False
        if self.min_new_bi_amplitude is not None and abs(latest_bi) < self.min_new_bi_amplitude:
            return False
        return True

    def _new_segment_opposes(self, chan: ChanExitSnapshot, ctx: ExitContext) -> bool:
        if chan.prev_high is None and chan.prev_low is None:
            return True
        if ctx.is_long and chan.prev_low is not None:
            return ctx.low < chan.prev_low
        if not ctx.is_long and chan.prev_high is not None:
            return ctx.high > chan.prev_high
        return True


# ═══════════════════════════════════════════
# ChanSmallTurnExitRule — 小转大保护
# ═══════════════════════════════════════════

class ChanSmallTurnExitRule(ExitRule):
    """小转大（小级别反转引发大级别转折）的保护。

    缠论原理:
      「小转大」指次级别的反转力度足够强，导致本级别走势
      在没有背驰的情况下直接反转——这是最危险的情形。

    V2: 小转大始终 CLOSE_ALL（它是突发不可逆事件，不适合分级）。
      priority 调至 5 — chan_structure 内最优先，因为它比背驰更紧急。

    参数:
      amplitude_multiplier: 反向力度需超过平均振幅的倍数。默认 2.0。
      require_fractal_break: True 时要求同时击穿分型确认位。
    """

    category = "chan_structure"
    priority = 5

    def __init__(
        self,
        *,
        amplitude_multiplier: float = 2.0,
        require_fractal_break: bool = True,
        avg_amplitude_window: int = 20,
    ) -> None:
        if amplitude_multiplier <= 0:
            raise ValueError("amplitude_multiplier must be positive")
        if avg_amplitude_window < 5:
            raise ValueError("avg_amplitude_window must be at least 5")
        self.amplitude_multiplier = float(amplitude_multiplier)
        self.require_fractal_break = require_fractal_break
        self.avg_amplitude_window = avg_amplitude_window

    @property
    def rule_id(self) -> str:
        fb = "_fb" if self.require_fractal_break else ""
        return f"ChanSmallTurnExitRule_x{int(self.amplitude_multiplier)}{fb}"

    def check(self, ctx: ExitContext) -> ExitSignal | None:
        chan = ctx.chan
        if chan is None:
            return None

        abnormal = self._is_abnormal_amplitude(chan, ctx)
        if not abnormal:
            return None

        if self.require_fractal_break:
            if not self._fractal_is_broken(chan, ctx):
                return None

        return ExitSignal(
            rule_id=self.rule_id,
            reason_code="chan_small_turn",
            exit_price=ctx.close,
            description=self._build_description(chan),
        )

    def _is_abnormal_amplitude(self, chan: ChanExitSnapshot, ctx: ExitContext) -> bool:
        if chan.avg_amplitude_20 is None or chan.bar_amplitude is None:
            return False
        if chan.avg_amplitude_20 <= 0:
            return False
        amp_ratio = chan.bar_amplitude / chan.avg_amplitude_20
        if amp_ratio < self.amplitude_multiplier:
            return False
        is_long = ctx.is_long
        is_bearish = ctx.close < ctx.open
        is_bullish = ctx.close > ctx.open
        if is_long and not is_bearish:
            return False
        if not is_long and not is_bullish:
            return False
        return True

    def _fractal_is_broken(self, chan: ChanExitSnapshot, ctx: ExitContext) -> bool:
        if chan.fractal_break_price is None:
            return False
        if ctx.is_long:
            return ctx.low < chan.fractal_break_price
        return ctx.high > chan.fractal_break_price

    def _build_description(self, chan: ChanExitSnapshot) -> str:
        amp_ratio = (
            (chan.bar_amplitude / chan.avg_amplitude_20)
            if (chan.bar_amplitude is not None and chan.avg_amplitude_20 is not None
                and chan.avg_amplitude_20 > 0)
            else 0.0
        )
        return (
            f"缠论小转大出场: 振幅倍率={amp_ratio:.1f}"
            f"{', 分型破位' if self.require_fractal_break else ''}"
        )


# ── helper ──────────────────────────────────

def _hold(rule: ExitRule) -> ExitDirective:
    return ExitDirective(
        rule_id=rule.rule_id,
        reason_code="none",
        action=ExitAction.HOLD,
    )
