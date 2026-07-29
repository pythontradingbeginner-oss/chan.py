"""仓位管理模块 —— 可插拔的仓位规模计算器。

设计原则：
  - Sizer 负责计算「该下多少手」
  - 与信号管线无关——只消费 SignalDecision 的锚点（entry_price、initial_stop_price）
  - 支持三种模式：fixed、atr、fixed_fractional
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


# ═══════════════════════════════════════════
# Sizer 抽象基类
# ═══════════════════════════════════════════


class Sizer(ABC):
    """仓位规模计算器抽象基类。"""

    @abstractmethod
    def calculate(
        self,
        *,
        initial_stop_price: float,
        entry_price: float,
        atr: float | None = None,
    ) -> int:
        """返回应开仓的手数（整数）。

        Args:
            initial_stop_price: 入场计划的初始止损价
            entry_price: 计划入场价
            atr: 当前 ATR 值（method=atr 时必传）
        """
        ...


# ═══════════════════════════════════════════
# FixedSizer — 固定手数
# ═══════════════════════════════════════════


class FixedSizer(Sizer):
    """固定手数——使用 config.sizing.lots 指定的手数。"""

    def __init__(self, lots: int = 1) -> None:
        if lots < 1:
            raise ValueError("lots must be >= 1")
        self.lots = lots

    def calculate(self, **kwargs: object) -> int:
        return self.lots


# ═══════════════════════════════════════════
# FixedFractionalSizer — 固定比例风险
# ═══════════════════════════════════════════


class FixedFractionalSizer(Sizer):
    """基于固定比例风险的仓位计算。

    公式: lots = floor(capital * risk_pct / (stop_distance * contract_multiplier))

    例如: capital=100,000, risk_pct=2%, stop_distance=180pts, multiplier=10
      lots = 100000 * 0.02 / (180 * 10) = 2000 / 1800 = 1
    """

    def __init__(
        self,
        capital: float = 100_000,
        risk_pct: float = 0.02,
        contract_multiplier: float = 10.0,
    ) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        if not 0 < risk_pct < 1:
            raise ValueError("risk_pct must be between 0 and 1")
        self.capital = float(capital)
        self.risk_pct = float(risk_pct)
        self.multiplier = float(contract_multiplier)

    def calculate(
        self,
        *,
        initial_stop_price: float,
        entry_price: float,
        **kwargs: object,
    ) -> int:
        stop_distance = abs(entry_price - initial_stop_price)
        if stop_distance <= 0:
            return 1  # 止损价位无效时回退到 1 手

        risk_amount = self.capital * self.risk_pct
        lots = risk_amount / (stop_distance * self.multiplier)
        return max(1, int(np.floor(lots)))


# ═══════════════════════════════════════════
# ATRSizer — 基于波动率的仓位
# ═══════════════════════════════════════════


class ATRSizer(Sizer):
    """基于 ATR 波动率的仓位计算。

    公式: lots = floor(capital * risk_pct / (atr * atr_multiplier * contract_multiplier))

    例如: capital=100,000, risk_pct=2%, atr=30, atr_multiplier=2, multiplier=10
      stop_distance = 30 * 2 = 60
      lots = 100000 * 0.02 / (60 * 10) = 2000 / 600 = 3
    """

    def __init__(
        self,
        capital: float = 100_000,
        risk_pct: float = 0.02,
        atr_multiplier: float = 2.0,
        contract_multiplier: float = 10.0,
        min_atr: float | None = None,
    ) -> None:
        if capital <= 0:
            raise ValueError("capital must be positive")
        if not 0 < risk_pct < 1:
            raise ValueError("risk_pct must be between 0 and 1")
        if atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive")
        self.capital = float(capital)
        self.risk_pct = float(risk_pct)
        self.atr_multiplier = float(atr_multiplier)
        self.multiplier = float(contract_multiplier)
        self.min_atr = float(min_atr) if min_atr is not None else None

    def calculate(
        self,
        *,
        atr: float | None = None,
        **kwargs: object,
    ) -> int:
        if atr is None or atr <= 0:
            if self.min_atr is not None:
                atr = self.min_atr
            else:
                return 1  # ATR 不可用时回退到 1 手

        stop_distance = atr * self.atr_multiplier
        risk_amount = self.capital * self.risk_pct
        lots = risk_amount / (stop_distance * self.multiplier)
        return max(1, int(np.floor(lots)))


# ═══════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════


def make_sizer(config) -> Sizer:
    """从 StrategyConfig.sizing 构建 Sizer 实例。"""
    method = config.method
    if method == "fixed":
        return FixedSizer(lots=config.lots)
    elif method == "atr":
        return ATRSizer(
            capital=config.capital,
            risk_pct=config.risk_pct,
            atr_multiplier=config.atr_multiplier,
            contract_multiplier=config.contract_multiplier,
        )
    elif method == "fixed_fractional":
        return FixedFractionalSizer(
            capital=config.capital,
            risk_pct=config.risk_pct,
            contract_multiplier=config.contract_multiplier,
        )
    else:
        raise ValueError(f"未知仓位方法: {method}，可用: fixed/atr/fixed_fractional")
