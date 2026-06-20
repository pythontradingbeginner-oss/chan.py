from __future__ import annotations

from dataclasses import dataclass

from .strategy import StrategySignal


@dataclass
class PositionState:
    position: int = 0
    avg_price: float | None = None
    realized_points: float = 0.0

    def equity_points(self, mark_price: float) -> float:
        if self.position == 0 or self.avg_price is None:
            return self.realized_points
        return self.realized_points + self.position * (mark_price - self.avg_price)


@dataclass(frozen=True)
class Fill:
    timestamp: object
    action: str
    previous_position: int
    target_position: int
    quantity_delta: int
    signal_price: float
    fill_price: float
    realized_points: float
    equity_points: float
    reason: str
    bsp_type: str
    active_symbol: str | None = None


class SimulatedExecutionEngine:
    def __init__(self, *, fee_points: float = 0.0, slippage_points: float = 0.0) -> None:
        self.fee_points = float(fee_points)
        self.slippage_points = float(slippage_points)
        self.state = PositionState()
        self.fills: list[Fill] = []

    def execute(self, signal: StrategySignal) -> Fill | None:
        previous_position = self.state.position
        target_position = signal.target_position
        quantity_delta = target_position - previous_position
        if quantity_delta == 0:
            return None

        fill_price = self._fill_price(signal.price, quantity_delta)
        self.state.realized_points -= self.fee_points * abs(quantity_delta)
        self._apply_position_change(target_position, fill_price)
        fill = Fill(
            timestamp=signal.timestamp,
            action=signal.action,
            previous_position=previous_position,
            target_position=target_position,
            quantity_delta=quantity_delta,
            signal_price=signal.price,
            fill_price=fill_price,
            realized_points=self.state.realized_points,
            equity_points=self.state.equity_points(fill_price),
            reason=signal.reason,
            bsp_type=signal.bsp_type,
            active_symbol=signal.active_symbol,
        )
        self.fills.append(fill)
        return fill

    def mark_to_market(self, price: float) -> float:
        return self.state.equity_points(float(price))

    def _fill_price(self, signal_price: float, quantity_delta: int) -> float:
        if quantity_delta > 0:
            return float(signal_price) + self.slippage_points
        return float(signal_price) - self.slippage_points

    def _apply_position_change(self, target_position: int, fill_price: float) -> None:
        previous_position = self.state.position
        previous_avg = self.state.avg_price
        if previous_position != 0 and previous_avg is not None:
            if target_position == 0 or _sign(target_position) != _sign(previous_position):
                self.state.realized_points += previous_position * (fill_price - previous_avg)

        self.state.position = target_position
        if target_position == 0:
            self.state.avg_price = None
        elif previous_position == 0 or _sign(target_position) != _sign(previous_position):
            self.state.avg_price = fill_price


def _sign(value: int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
