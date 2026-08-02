"""Canonical decision and audit objects shared by backtest, CTA and live."""

from __future__ import annotations

from dataclasses import dataclass, replace

from signal_core.models import SignalAssessment, SignalDecision, SignalEvent
from strategy_policy.position import PositionContext

from .strategy import StrategySignal


@dataclass(frozen=True, slots=True)
class DecisionTraceRecord:
    """Runtime-neutral decision fields used for cross-runtime parity checks."""

    timestamp: object
    event_id: str
    signal_key: str
    policy_id: str
    accepted: bool
    reason_codes: tuple[str, ...]
    direction: str
    target_position: int
    position_size: int
    entry_price: float | None
    setup_invalidation_price: float | None
    execution_stop_price: float | None
    bsp_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "signal_key": self.signal_key,
            "policy_id": self.policy_id,
            "accepted": self.accepted,
            "reason_codes": self.reason_codes,
            "direction": self.direction,
            "target_position": self.target_position,
            "position_size": self.position_size,
            "entry_price": self.entry_price,
            "setup_invalidation_price": self.setup_invalidation_price,
            "execution_stop_price": self.execution_stop_price,
            "bsp_type": self.bsp_type,
        }


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """One complete strategy decision, including its executable signal."""

    signal: StrategySignal
    decision: SignalDecision
    event: SignalEvent | None = None
    assessment: SignalAssessment | None = None

    @property
    def accepted(self) -> bool:
        return self.decision.accepted

    @property
    def bsp_type(self) -> str:
        return self.signal.bsp_type

    @property
    def grade(self) -> str:
        return self.assessment.grade.value if self.assessment is not None else "standard"

    @property
    def structural_score(self) -> float:
        if self.assessment is None or self.assessment.structural_score is None:
            return 0.0
        return float(self.assessment.structural_score)

    @property
    def event_id(self) -> str:
        return self.event.event_id if self.event is not None else self.decision.event_id

    @property
    def signal_key(self) -> str:
        return self.event.signal_key if self.event is not None else ""

    @property
    def lots(self) -> int:
        return abs(self.signal.target_position) if self.accepted else 0

    def position_from_fill(
        self,
        *,
        fill_price: float,
        fill_volume: int,
        fill_time: object,
        entry_bar: int | None = None,
        active_symbol: str | None = None,
    ) -> PositionContext:
        return PositionContext.from_decision_fill(
            decision=self.decision,
            event=self.event,
            assessment=self.assessment,
            target_position=self.signal.target_position,
            fill_price=fill_price,
            fill_volume=fill_volume,
            fill_time=fill_time,
            entry_bar=entry_bar,
            bsp_type=self.bsp_type,
            active_symbol=active_symbol or self.signal.active_symbol,
        )

    def with_signal(self, signal: StrategySignal) -> TradeIntent:
        return replace(self, signal=signal)

    def to_trace_record(self) -> DecisionTraceRecord:
        if self.event is not None:
            direction = self.event.direction.value
        elif self.signal.target_position > 0:
            direction = "long"
        elif self.signal.target_position < 0:
            direction = "short"
        else:
            direction = "flat"

        setup = self.decision.setup_invalidation_price
        if setup is None:
            setup = self.decision.invalidation_price
        stop = self.decision.execution_stop_price
        if stop is None:
            stop = self.decision.initial_stop_price

        return DecisionTraceRecord(
            timestamp=self.signal.timestamp,
            event_id=self.event_id,
            signal_key=self.signal_key,
            policy_id=self.decision.policy_id,
            accepted=self.accepted,
            reason_codes=self.decision.reason_codes,
            direction=direction,
            target_position=self.signal.target_position if self.accepted else 0,
            position_size=self.lots,
            entry_price=self.decision.entry_price_hint,
            setup_invalidation_price=setup,
            execution_stop_price=stop,
            bsp_type=self.bsp_type,
        )


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """A TradeIntent waiting for one or more opening fills."""

    intent: TradeIntent
    filled_volume: int = 0

    @property
    def planned_volume(self) -> int:
        return self.intent.lots

    @property
    def remaining_volume(self) -> int:
        return max(0, self.planned_volume - self.filled_volume)

    @property
    def complete(self) -> bool:
        return self.remaining_volume == 0

    def apply_fill(self, fill_volume: int) -> PendingEntry:
        if fill_volume < 1:
            raise ValueError("fill_volume must be >= 1")
        return replace(
            self,
            filled_volume=min(self.planned_volume, self.filled_volume + fill_volume),
        )
