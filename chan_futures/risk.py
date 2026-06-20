from __future__ import annotations

from dataclasses import dataclass

from .strategy import StrategySignal


@dataclass(frozen=True)
class RiskConfig:
    max_abs_position: int = 1
    max_loss_points: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = "approved"


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def approve(self, signal: StrategySignal, *, realized_points: float) -> RiskDecision:
        if abs(signal.target_position) > self.config.max_abs_position:
            return RiskDecision(False, "max_abs_position")
        if (
            self.config.max_loss_points is not None
            and realized_points <= -abs(self.config.max_loss_points)
        ):
            return RiskDecision(False, "max_loss_points")
        return RiskDecision(True)
