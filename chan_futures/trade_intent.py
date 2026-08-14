"""Canonical decision and audit objects shared by backtest, CTA and live."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Any

import pandas as pd

from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
    SignalState,
)
from strategy_policy.position import PositionContext
from strategy_policy.qingpai_decomposition import (
    CenterSnapshot,
    DecompositionLifecycle,
    DecompositionSnapshot,
    QingpaiRegime,
    StructureDirection,
)

from .multi_level import (
    MultiLevelDecisionContext,
    ParentDirection,
    ParentStructureSnapshot,
    SubLevelSignalSnapshot,
)
from .qingpai_momentum import MomentumConfirmation, MomentumStatus
from .strategy import StrategySignal


AUDIT_SCHEMA_VERSION = "qingpai-p0-audit-v1"
SETUP_CANDIDATE_SCHEMA_VERSION = "setup-candidate-v1"
TRADE_INTENT_RUNTIME_VERSION = "trade-intent-runtime-v1"


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    """Research-only setup identity; it must not affect strategy behavior."""

    candidate_id: str
    contract_epoch: str
    timeframe: str
    direction: str
    root_bi_idx: int
    setup_family: str
    schema_version: str = SETUP_CANDIDATE_SCHEMA_VERSION

    def to_flat_dict(self) -> dict[str, object]:
        return {
            "setup_candidate_schema_version": self.schema_version,
            "setup_candidate_id": self.candidate_id,
            "setup_contract_epoch": self.contract_epoch,
            "setup_timeframe": self.timeframe,
            "setup_direction": self.direction,
            "setup_root_bi_idx": self.root_bi_idx,
            "setup_family": self.setup_family,
            "setup_state": "observed_only",
        }


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
    audit_schema_version: str = AUDIT_SCHEMA_VERSION
    raw_bi_idx: int | None = None
    raw_klu_idx: int | None = None
    raw_bsp_type: str = ""
    raw_is_buy: bool | None = None
    setup_candidate_schema_version: str = SETUP_CANDIDATE_SCHEMA_VERSION
    setup_candidate_id: str = ""
    setup_contract_epoch: str = ""
    setup_timeframe: str = ""
    setup_direction: str = ""
    setup_root_bi_idx: int | None = None
    setup_family: str = ""
    setup_state: str = "observed_only"
    risk_session_key: str | None = None
    risk_session_observed_at: object | None = None
    risk_session_source: str = ""
    equity_value: float | None = None
    equity_unit: str = "account_currency"
    equity_source: str = ""
    equity_scope: str = ""
    max_drawdown_capability: str = ""

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
            "audit_schema_version": self.audit_schema_version,
            "raw_bi_idx": self.raw_bi_idx,
            "raw_klu_idx": self.raw_klu_idx,
            "raw_bsp_type": self.raw_bsp_type,
            "raw_is_buy": self.raw_is_buy,
            "setup_candidate_schema_version": self.setup_candidate_schema_version,
            "setup_candidate_id": self.setup_candidate_id,
            "setup_contract_epoch": self.setup_contract_epoch,
            "setup_timeframe": self.setup_timeframe,
            "setup_direction": self.setup_direction,
            "setup_root_bi_idx": self.setup_root_bi_idx,
            "setup_family": self.setup_family,
            "setup_state": self.setup_state,
            "risk_session_key": self.risk_session_key,
            "risk_session_observed_at": self.risk_session_observed_at,
            "risk_session_source": self.risk_session_source,
            "equity_value": self.equity_value,
            "equity_unit": self.equity_unit,
            "equity_source": self.equity_source,
            "equity_scope": self.equity_scope,
            "max_drawdown_capability": self.max_drawdown_capability,
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

    @property
    def setup_candidate(self) -> SetupCandidate:
        event = self.event
        event_direction = getattr(event, "direction", None)
        direction = getattr(event_direction, "value", event_direction)
        if not direction:
            if self.signal.target_position > 0 or "long" in self.signal.action:
                direction = "long"
            elif self.signal.target_position < 0 or "short" in self.signal.action:
                direction = "short"
            else:
                direction = "flat"
        primary_bsp = str(
            getattr(event, "primary_bsp", "") or self.signal.bsp_type or "unknown"
        )
        related_bsp1 = getattr(event, "related_bsp1_bi_idx", None)
        root_bi_idx = int(
            related_bsp1
            if related_bsp1 is not None
            else getattr(event, "bi_idx", self.signal.bsp_bi_idx)
        )
        contract_epoch = str(
            self.signal.active_symbol
            or getattr(event, "contract", "")
            or "unknown"
        )
        timeframe = str(getattr(event, "timeframe", "") or "unknown")
        # OPEN-006 has not decided BSP-family merging.  Keeping the primary BSP
        # explicit prevents this observation-only identity from implying it.
        setup_family = f"unresolved_primary_bsp:{primary_bsp}"
        payload = "|".join(
            (
                SETUP_CANDIDATE_SCHEMA_VERSION,
                contract_epoch,
                timeframe,
                str(direction),
                str(root_bi_idx),
                setup_family,
            )
        )
        candidate_id = "setup_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return SetupCandidate(
            candidate_id=candidate_id,
            contract_epoch=contract_epoch,
            timeframe=timeframe,
            direction=str(direction),
            root_bi_idx=root_bi_idx,
            setup_family=setup_family,
        )

    def audit_identity_fields(self) -> dict[str, object]:
        direction = self.setup_candidate.direction
        return {
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "raw_bi_idx": self.signal.bsp_bi_idx,
            "raw_klu_idx": self.signal.bsp_klu_idx,
            "raw_bsp_type": self.signal.bsp_type,
            "raw_is_buy": direction == "long",
            **self.setup_candidate.to_flat_dict(),
        }

    def position_from_fill(
        self,
        *,
        fill_price: float,
        fill_volume: int,
        fill_time: object,
        entry_bar: int | None = None,
        active_symbol: str | None = None,
        position_id: str = "",
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
            position_id=position_id,
            **self.setup_candidate.to_flat_dict(),
        )

    def with_signal(self, signal: StrategySignal) -> TradeIntent:
        return replace(self, signal=signal)

    def for_open_leg(self, quantity: int) -> TradeIntent:
        """Return the canonical executable intent for one opening leg."""
        if isinstance(quantity, bool):
            raise ValueError("open leg quantity must be a positive integer")
        normalized_quantity = int(quantity)
        if normalized_quantity < 1 or normalized_quantity != quantity:
            raise ValueError("open leg quantity must be a positive integer")
        target_sign = 1 if self.signal.target_position > 0 else -1
        return self.with_signal(
            replace(
                self.signal,
                action="open_long" if target_sign > 0 else "open_short",
                target_position=target_sign * normalized_quantity,
            )
        )

    def with_decomposition(
        self,
        decomposition: DecompositionSnapshot | None,
    ) -> TradeIntent:
        return replace(self, decomposition=decomposition)

    def to_runtime_snapshot(self) -> dict[str, object]:
        """Serialize the complete executable intent for restart recovery."""
        return {
            "schema_version": TRADE_INTENT_RUNTIME_VERSION,
            "signal": _json_value(self.signal),
            "decision": _json_value(self.decision),
            "event": _json_value(self.event),
            "assessment": _json_value(self.assessment),
            "decomposition": _json_value(self.decomposition),
            "multi_level": _json_value(self.multi_level),
            "momentum": _json_value(self.momentum),
        }

    @classmethod
    def from_runtime_snapshot(cls, snapshot: dict[str, object]) -> "TradeIntent":
        if snapshot.get("schema_version") != TRADE_INTENT_RUNTIME_VERSION:
            raise ValueError("trade_intent_runtime_version_mismatch")
        return cls(
            signal=_restore_signal(_mapping(snapshot, "signal")),
            decision=_restore_decision(_mapping(snapshot, "decision")),
            event=_restore_event(_optional_mapping(snapshot.get("event"))),
            assessment=_restore_assessment(
                _optional_mapping(snapshot.get("assessment"))
            ),
            decomposition=_restore_decomposition(
                _optional_mapping(snapshot.get("decomposition"))
            ),
            multi_level=_restore_multi_level(
                _optional_mapping(snapshot.get("multi_level"))
            ),
            momentum=_restore_momentum(
                _optional_mapping(snapshot.get("momentum"))
            ),
        )

    def to_trace_record(
        self,
        *,
        risk_session_key: str | None = None,
        risk_session_observed_at: object | None = None,
        risk_session_source: str = "",
        equity_value: float | None = None,
        equity_source: str = "",
        equity_scope: str = "",
        max_drawdown_capability: str = "",
    ) -> DecisionTraceRecord:
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
            **self.audit_identity_fields(),
            risk_session_key=risk_session_key,
            risk_session_observed_at=risk_session_observed_at,
            risk_session_source=risk_session_source,
            equity_value=equity_value,
            equity_source=equity_source,
            equity_scope=equity_scope,
            max_drawdown_capability=max_drawdown_capability,
        )


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """A TradeIntent waiting for one or more opening fills."""

    intent: TradeIntent
    filled_volume: int = 0
    probe_intent_id: str = ""

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


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    item = getattr(value, "item", None)
    if callable(item):
        return _json_value(item())
    raise TypeError(f"trade intent value is not JSON serializable: {type(value).__name__}")


def _mapping(parent: dict[str, object], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"trade_intent_{key}_missing")
    return dict(value)


def _optional_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("trade_intent_optional_payload_invalid")
    return dict(value)


def _time(value: object) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def _restore_signal(data: dict[str, Any]) -> StrategySignal:
    return StrategySignal(**{**data, "timestamp": _time(data["timestamp"])})


def _restore_decision(data: dict[str, Any]) -> SignalDecision:
    return SignalDecision(
        **{
            **data,
            "reason_codes": tuple(data.get("reason_codes", [])),
            "decided_at": _time(data["decided_at"]),
        }
    )


def _restore_event(data: dict[str, Any] | None) -> SignalEvent | None:
    if data is None:
        return None
    return SignalEvent(
        **{
            **data,
            "bar_end_time": _time(data["bar_end_time"]),
            "available_at": _time(data["available_at"]),
            "state": SignalState(data["state"]),
            "direction": SignalDirection(data["direction"]),
            "bsp_types": tuple(data.get("bsp_types", [])),
            "features": dict(data.get("features") or {}),
        }
    )


def _restore_assessment(data: dict[str, Any] | None) -> SignalAssessment | None:
    if data is None:
        return None
    return SignalAssessment(
        **{
            **data,
            "grade": ScoreGrade(data["grade"]),
            "hard_blockers": tuple(data.get("hard_blockers", [])),
            "component_scores": dict(data.get("component_scores") or {}),
            "computed_at": _time(data["computed_at"]),
        }
    )


def _restore_center(data: dict[str, Any]) -> CenterSnapshot:
    return CenterSnapshot(**{**data, "direction": StructureDirection(data["direction"])})


def _restore_decomposition(data: dict[str, Any] | None) -> DecompositionSnapshot | None:
    if data is None:
        return None
    return DecompositionSnapshot(
        **{
            **data,
            "regime": QingpaiRegime(data["regime"]),
            "direction": StructureDirection(data["direction"]),
            "lifecycle": DecompositionLifecycle(data["lifecycle"]),
            "observed_at": _time(data["observed_at"]),
            "available_at": _time(data["available_at"]),
            "centers": tuple(_restore_center(value) for value in data.get("centers", [])),
            "reason_codes": tuple(data.get("reason_codes", [])),
        }
    )


def _restore_parent(data: dict[str, Any] | None) -> ParentStructureSnapshot | None:
    if data is None:
        return None
    return ParentStructureSnapshot(
        **{
            **data,
            "direction": ParentDirection(data["direction"]),
            "structure_begin_time": _time(data["structure_begin_time"]),
            "structure_end_time": _time(data["structure_end_time"]),
            "available_at": _time(data["available_at"]),
        }
    )


def _restore_child(data: dict[str, Any]) -> SubLevelSignalSnapshot:
    return SubLevelSignalSnapshot(
        **{
            **data,
            "bsp_types": tuple(data.get("bsp_types", [])),
            "signal_time": _time(data["signal_time"]),
            "bi_begin_time": _time(data["bi_begin_time"]),
            "bi_end_time": _time(data["bi_end_time"]),
            "available_at": _time(data["available_at"]),
        }
    )


def _restore_multi_level(data: dict[str, Any] | None) -> MultiLevelDecisionContext | None:
    if data is None:
        return None
    return MultiLevelDecisionContext(
        **{
            **data,
            "decision_time": _time(data["decision_time"]),
            "reason_codes": tuple(data.get("reason_codes", [])),
            "parent_snapshot": _restore_parent(data.get("parent_snapshot")),
            "child_window_begin": (
                _time(data["child_window_begin"])
                if data.get("child_window_begin") is not None
                else None
            ),
            "child_window_end": (
                _time(data["child_window_end"])
                if data.get("child_window_end") is not None
                else None
            ),
            "child_matches": tuple(
                _restore_child(value) for value in data.get("child_matches", [])
            ),
            "parent_last_observed_at": (
                _time(data["parent_last_observed_at"])
                if data.get("parent_last_observed_at") is not None
                else None
            ),
            "child_last_observed_at": (
                _time(data["child_last_observed_at"])
                if data.get("child_last_observed_at") is not None
                else None
            ),
        }
    )


def _restore_momentum(data: dict[str, Any] | None) -> MomentumConfirmation | None:
    if data is None:
        return None
    return MomentumConfirmation(
        **{
            **data,
            "observed_at": _time(data["observed_at"]),
            "available_at": _time(data["available_at"]),
            "status": MomentumStatus(data["status"]),
            "reason_codes": tuple(data.get("reason_codes", [])),
        }
    )
