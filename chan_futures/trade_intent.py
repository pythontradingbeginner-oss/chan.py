"""Canonical decision and audit objects shared by backtest, CTA and live."""

from __future__ import annotations

from dataclasses import dataclass, replace

from signal_core.models import SignalAssessment, SignalDecision, SignalEvent
from strategy_policy.position import PositionContext
from strategy_policy.qingpai_decomposition import DecompositionSnapshot

from .multi_level import MultiLevelDecisionContext
from .qingpai_momentum import MomentumConfirmation
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
    decomposition_id: str = ""
    decomposition_revision: int | None = None
    regime: str = "unclassified"
    regime_direction: str = ""
    regime_lifecycle: str = ""
    multi_level_accepted: bool | None = None
    multi_level_time_honest: bool | None = None
    multi_level_reason_codes: tuple[str, ...] = ()
    parent_direction: str = ""
    parent_structure_id: str = ""
    parent_available_at: object | None = None
    parent_action: str = ""
    child_confirmed: bool | None = None
    child_match_count: int = 0
    child_match_ids: tuple[str, ...] = ()
    child_window_begin: object | None = None
    child_window_end: object | None = None
    momentum_status: str = ""
    momentum_accepted: bool | None = None
    momentum_reason_codes: tuple[str, ...] = ()
    momentum_available_at: object | None = None
    macd_area_ratio: float | None = None
    macd_peak_ratio: float | None = None
    price_strength_ratio: float | None = None
    price_slope_ratio: float | None = None
    effective_extension: float | None = None
    histogram_state: str = ""

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
            "decomposition_id": self.decomposition_id,
            "decomposition_revision": self.decomposition_revision,
            "regime": self.regime,
            "regime_direction": self.regime_direction,
            "regime_lifecycle": self.regime_lifecycle,
            "multi_level_accepted": self.multi_level_accepted,
            "multi_level_time_honest": self.multi_level_time_honest,
            "multi_level_reason_codes": self.multi_level_reason_codes,
            "parent_direction": self.parent_direction,
            "parent_structure_id": self.parent_structure_id,
            "parent_available_at": self.parent_available_at,
            "parent_action": self.parent_action,
            "child_confirmed": self.child_confirmed,
            "child_match_count": self.child_match_count,
            "child_match_ids": self.child_match_ids,
            "child_window_begin": self.child_window_begin,
            "child_window_end": self.child_window_end,
            "momentum_status": self.momentum_status,
            "momentum_accepted": self.momentum_accepted,
            "momentum_reason_codes": self.momentum_reason_codes,
            "momentum_available_at": self.momentum_available_at,
            "macd_area_ratio": self.macd_area_ratio,
            "macd_peak_ratio": self.macd_peak_ratio,
            "price_strength_ratio": self.price_strength_ratio,
            "price_slope_ratio": self.price_slope_ratio,
            "effective_extension": self.effective_extension,
            "histogram_state": self.histogram_state,
        }


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """One complete strategy decision, including its executable signal."""

    signal: StrategySignal
    decision: SignalDecision
    event: SignalEvent | None = None
    assessment: SignalAssessment | None = None
    decomposition: DecompositionSnapshot | None = None
    multi_level: MultiLevelDecisionContext | None = None
    momentum: MomentumConfirmation | None = None

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

    def with_decomposition(
        self,
        decomposition: DecompositionSnapshot | None,
    ) -> TradeIntent:
        return replace(self, decomposition=decomposition)

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
        multi_level = self.multi_level
        parent = multi_level.parent_snapshot if multi_level is not None else None
        momentum = self.momentum

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
            decomposition_id=(
                self.decomposition.decomposition_id if self.decomposition else ""
            ),
            decomposition_revision=(
                self.decomposition.revision if self.decomposition else None
            ),
            regime=(
                self.decomposition.regime.value
                if self.decomposition
                else "unclassified"
            ),
            regime_direction=(
                self.decomposition.direction.value if self.decomposition else ""
            ),
            regime_lifecycle=(
                self.decomposition.lifecycle.value if self.decomposition else ""
            ),
            multi_level_accepted=(multi_level.accepted if multi_level else None),
            multi_level_time_honest=(multi_level.time_honest if multi_level else None),
            multi_level_reason_codes=(multi_level.reason_codes if multi_level else ()),
            parent_direction=(parent.direction.value if parent else ""),
            parent_structure_id=(parent.snapshot_id if parent else ""),
            parent_available_at=(parent.available_at if parent else None),
            parent_action=(multi_level.parent_action if multi_level else ""),
            child_confirmed=(multi_level.child_confirmed if multi_level else None),
            child_match_count=(len(multi_level.child_matches) if multi_level else 0),
            child_match_ids=(
                tuple(match.signal_id for match in multi_level.child_matches)
                if multi_level
                else ()
            ),
            child_window_begin=(multi_level.child_window_begin if multi_level else None),
            child_window_end=(multi_level.child_window_end if multi_level else None),
            momentum_status=(momentum.status.value if momentum else ""),
            momentum_accepted=(momentum.accepted if momentum else None),
            momentum_reason_codes=(momentum.reason_codes if momentum else ()),
            momentum_available_at=(momentum.available_at if momentum else None),
            macd_area_ratio=(momentum.area_ratio if momentum else None),
            macd_peak_ratio=(momentum.peak_ratio if momentum else None),
            price_strength_ratio=(
                momentum.price_strength_ratio if momentum else None
            ),
            price_slope_ratio=(momentum.slope_ratio if momentum else None),
            effective_extension=(momentum.effective_extension if momentum else None),
            histogram_state=(momentum.histogram_state if momentum else ""),
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
