"""风险管理模块 —— 有状态的风控检查。

风控维度：
  - max_abs_position: 最大持仓手数（始终生效）
  - max_loss_points: 累计最大亏损（全局，累计已实现亏损超限即停止）
  - daily_loss_limit: 日内亏损限额（当日累计已实现亏损超限则当日不再开仓）
  - max_consecutive_losses: 连续亏损笔数上限（达到后暂停，直到出现盈利）
  - max_drawdown_pct: 从权益峰值回撤超过阈值暂停开仓

RiskManager 已从无状态（仅 approve 检查）扩展为有状态对象：
  - 通过 on_fill() 更新内部追踪状态
  - 通过 approve() 做实时风控决策
  - 通过 get_state() 和 summary() 供审计
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .strategy import StrategySignal


# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════


@dataclass(frozen=True)
class RiskConfig:
    max_abs_position: int = 1
    max_loss_points: float | None = None           # 累计已实现亏损上限
    daily_loss_limit: float | None = None           # 日累计亏损上限
    max_consecutive_losses: int | None = None       # 连续亏损笔数上限
    max_drawdown_pct: float | None = None           # 峰值权益回撤百分比


# ═══════════════════════════════════════════
# RiskDecision
# ═══════════════════════════════════════════


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = "approved"


# ═══════════════════════════════════════════
# RiskManager
# ═══════════════════════════════════════════


class RiskManager:
    """有状态的风控管理器。

    用法:
        risk = RiskManager(RiskConfig(daily_loss_limit=200))
        for each fill:
            risk.on_fill(fill)   # 更新累计亏损和连续亏损计数
        decision = risk.approve(signal, current_equity)
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

        # ── 累计状态 ──
        self._realized_points: float = 0.0
        self._peak_equity: float = 0.0
        self._total_fills: int = 0

        # ── 日内状态 ──
        self._daily_realized: float = 0.0
        self._current_date: object = None

        # ── 连续亏损状态 ──
        self._consecutive_losses: int = 0
        self._last_fill_pnl: float | None = None

    # ── 核心接口 ──

    def approve(
        self,
        signal: StrategySignal,
        *,
        current_equity: float | None = None,
    ) -> RiskDecision:
        """在开仓/加仓前做风控检查。

        Args:
            signal: 策略信号
            current_equity: 当前权益（用于峰值回撤计算）
        """
        # 1. 持仓限制
        if abs(signal.target_position) > self.config.max_abs_position:
            return RiskDecision(False, "max_abs_position")

        # 2. 累计亏损限制
        if (
            self.config.max_loss_points is not None
            and self._realized_points <= -abs(self.config.max_loss_points)
        ):
            return RiskDecision(
                False,
                f"max_loss_points: realized={self._realized_points:.0f} <= "
                f"-{abs(self.config.max_loss_points):.0f}",
            )

        # 3. 日内亏损限制
        if (
            self.config.daily_loss_limit is not None
            and self._daily_realized <= -abs(self.config.daily_loss_limit)
        ):
            return RiskDecision(
                False,
                f"daily_loss_limit: daily_realized={self._daily_realized:.0f}",
            )

        # 4. 连续亏损限制
        if (
            self.config.max_consecutive_losses is not None
            and self._consecutive_losses >= self.config.max_consecutive_losses
        ):
            return RiskDecision(
                False,
                f"max_consecutive_losses: {self._consecutive_losses} >= "
                f"{self.config.max_consecutive_losses}",
            )

        # 5. 峰值回撤限制
        if self.config.max_drawdown_pct is not None and current_equity is not None:
            if self._peak_equity > 0:
                drawdown = (self._peak_equity - current_equity) / self._peak_equity
                if drawdown >= self.config.max_drawdown_pct:
                    return RiskDecision(
                        False,
                        f"max_drawdown: {drawdown:.2%} >= {self.config.max_drawdown_pct:.2%}",
                    )

        return RiskDecision(True)

    def on_fill(
        self,
        *,
        pnl_points: float,
        fill_time: object | None = None,
        current_equity: float | None = None,
    ) -> None:
        """成交后更新内部追踪状态。

        Args:
            pnl_points: 平仓实现的盈亏（点数）。开仓时传 0。
            fill_time: 成交时间（用于日内汇总）
            current_equity: 当前权益（用于峰值回撤追踪）
        """
        self._total_fills += 1
        self._realized_points += pnl_points

        # 日内汇总
        if fill_time is not None and self.config.daily_loss_limit is not None:
            fill_date = _resolve_date(fill_time)
            if self._current_date is None or fill_date != self._current_date:
                self._daily_realized = 0.0
                self._current_date = fill_date
            self._daily_realized += pnl_points

        # 连续亏损追踪
        if pnl_points < 0:
            self._consecutive_losses += 1
        elif pnl_points > 0:
            self._consecutive_losses = 0

        # 峰值权益追踪
        if current_equity is not None:
            if current_equity > self._peak_equity:
                self._peak_equity = current_equity

    # ── 状态查询 ──

    def get_state(self) -> dict:
        """返回当前风控状态（供序列化/审计）。"""
        return {
            "realized_points": self._realized_points,
            "peak_equity": self._peak_equity,
            "total_fills": self._total_fills,
            "daily_realized": self._daily_realized,
            "consecutive_losses": self._consecutive_losses,
        }

    def summary(self) -> str:
        """返回可读的风控状态摘要。"""
        lines = [
            "RiskManager Status:",
            f"  realized_points: {self._realized_points:.1f}",
            f"  peak_equity:     {self._peak_equity:.1f}",
            f"  total_fills:     {self._total_fills}",
        ]
        if self.config.daily_loss_limit is not None:
            lines.append(f"  daily_realized:  {self._daily_realized:.1f} "
                         f"(limit: {self.config.daily_loss_limit})")
        if self.config.max_consecutive_losses is not None:
            lines.append(f"  consecutive_losses: {self._consecutive_losses} "
                         f"(limit: {self.config.max_consecutive_losses})")
        if self.config.max_drawdown_pct is not None and self._peak_equity > 0:
            lines.append(f"  peak_equity:     {self._peak_equity:.1f}")
        return "\n".join(lines)


# ═══════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════


def _resolve_date(fill_time: object) -> object:
    """从各种时间类型中解析日期。"""
    if isinstance(fill_time, pd.Timestamp):
        return fill_time.date()
    if hasattr(fill_time, "date"):
        return fill_time.date()
    return fill_time
