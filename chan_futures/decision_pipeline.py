"""Shared signal decision kernel used by backtest, CTA and live runtimes.

The pipeline consumes immutable signal data and returns one auditable
``SignalDecision``. Runtime adapters remain responsible only for extracting the
event and executing an accepted decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
)
from strategy_policy.entry_policy import EntryPolicy, EntryPolicyConfig

from .strategy import StrategySignal


class DecisionMode(StrEnum):
    """Compatibility and Qingpai-safe entry modes."""

    LEGACY = "legacy"
    QINGPAI_STRICT = "qingpai_strict"


@dataclass(frozen=True, slots=True)
class DecisionPipelineConfig:
    policy_id: str = "chan_entry_v1"
    mode: DecisionMode = DecisionMode.LEGACY
    min_grade: ScoreGrade = ScoreGrade.STANDARD
    accepted_bsp_types: frozenset[str] = frozenset(
        {"1", "1p", "2", "2s", "3a", "3b"}
    )
    allow_short: bool = True
    require_confirmed: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.mode, str) and not isinstance(self.mode, DecisionMode):
            object.__setattr__(self, "mode", DecisionMode(self.mode))
        if isinstance(self.min_grade, str) and not isinstance(self.min_grade, ScoreGrade):
            object.__setattr__(self, "min_grade", ScoreGrade(self.min_grade))


class DecisionPipeline:
    """Make the final strategy decision from signal, event and assessment."""

    def __init__(self, config: DecisionPipelineConfig | None = None) -> None:
        self.config = config or DecisionPipelineConfig()
        strict = self.config.mode == DecisionMode.QINGPAI_STRICT
        self._policy = EntryPolicy(
            EntryPolicyConfig(
                policy_id=self.config.policy_id,
                min_grade=self.config.min_grade,
                require_confirmed=self.config.require_confirmed if strict else False,
                accepted_bsp_types=self.config.accepted_bsp_types,
                long_only=not self.config.allow_short,
                reject_hard_blockers=strict,
            )
        )

    def evaluate(
        self,
        signal: StrategySignal,
        event: SignalEvent | None,
        assessment: SignalAssessment | None,
    ) -> SignalDecision:
        """Return an accepted or rejected decision without executing anything."""
        if event is None or assessment is None:
            if self.config.mode == DecisionMode.LEGACY:
                return _build_decision(
                    event_id="",
                    policy_id=self.config.policy_id,
                    accepted=True,
                    reason_codes=("legacy_ungraded_passthrough",),
                    entry_price=float(signal.price),
                )
            missing = "signal_event_unavailable" if event is None else "assessment_unavailable"
            return _build_decision(
                event_id=event.event_id if event is not None else "",
                policy_id=self.config.policy_id,
                accepted=False,
                reason_codes=(missing,),
            )

        # A close-only signal reduces risk and must not be blocked by entry gates.
        if signal.target_position == 0 and signal.action in {"close_long", "close_short"}:
            return _build_decision(
                event_id=event.event_id,
                policy_id=self.config.policy_id,
                accepted=True,
                reason_codes=("risk_reducing_close",),
                entry_price=float(signal.price),
            )

        decision = self._policy.evaluate(event, assessment)
        invariant_failures = self._check_invariants(signal, event, assessment)
        if not invariant_failures:
            return decision

        return replace(
            decision,
            accepted=False,
            reason_codes=decision.reason_codes + tuple(invariant_failures),
            entry_price_hint=None,
            invalidation_price=None,
            initial_stop_price=None,
            position_size_hint=None,
        )

    @staticmethod
    def _check_invariants(
        signal: StrategySignal,
        event: SignalEvent,
        assessment: SignalAssessment,
    ) -> list[str]:
        reasons: list[str] = []
        if assessment.event_id != event.event_id:
            reasons.append("assessment_event_mismatch")

        signal_types = {part.strip() for part in signal.bsp_type.split(",") if part.strip()}
        if signal_types and not signal_types.intersection(event.bsp_types):
            reasons.append("signal_bsp_mismatch")

        if signal.target_position != 0:
            expected = (
                SignalDirection.LONG
                if signal.target_position > 0
                else SignalDirection.SHORT
            )
            if event.direction != expected:
                reasons.append("signal_direction_mismatch")

        decision_time = _as_datetime(signal.timestamp)
        if decision_time is not None and not _is_available(event.available_at, decision_time):
            reasons.append("event_not_available")
        return reasons


def _build_decision(
    *,
    event_id: str,
    policy_id: str,
    accepted: bool,
    reason_codes: tuple[str, ...],
    entry_price: float | None = None,
) -> SignalDecision:
    return SignalDecision(
        decision_id=str(uuid.uuid4())[:12],
        event_id=event_id,
        policy_id=policy_id,
        accepted=accepted,
        reason_codes=reason_codes,
        entry_price_hint=entry_price if accepted else None,
        invalidation_price=None,
        initial_stop_price=None,
        position_size_hint=1.0 if accepted else None,
        decided_at=datetime.now(),
    )


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    converter = getattr(value, "to_pydatetime", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, datetime) else None
    return None


def _is_available(available_at: datetime, decision_time: datetime) -> bool:
    try:
        return available_at <= decision_time
    except TypeError:
        # Compare wall-clock values when one side is timezone-aware and the other is not.
        return available_at.replace(tzinfo=None) <= decision_time.replace(tzinfo=None)
