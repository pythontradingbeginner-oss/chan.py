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
from math import floor

from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
)
from strategy_policy.entry_policy import EntryPolicy, EntryPolicyConfig

from .sizing import Sizer
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
    max_abs_position: int | None = None
    contract_multiplier: float = 1.0
    margin_rate: float = 0.0
    max_margin_utilization: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.mode, str) and not isinstance(self.mode, DecisionMode):
            object.__setattr__(self, "mode", DecisionMode(self.mode))
        if isinstance(self.min_grade, str) and not isinstance(self.min_grade, ScoreGrade):
            object.__setattr__(self, "min_grade", ScoreGrade(self.min_grade))
        if self.max_abs_position is not None and self.max_abs_position < 1:
            raise ValueError("max_abs_position must be >= 1")
        if self.contract_multiplier <= 0:
            raise ValueError("contract_multiplier must be positive")
        if not 0 <= self.margin_rate < 1:
            raise ValueError("margin_rate must be in [0, 1)")
        if not 0 < self.max_margin_utilization <= 1:
            raise ValueError("max_margin_utilization must be in (0, 1]")


class DecisionPipeline:
    """Make the final strategy decision from signal, event and assessment."""

    def __init__(
        self,
        config: DecisionPipelineConfig | None = None,
        *,
        sizer: Sizer | None = None,
    ) -> None:
        self.config = config or DecisionPipelineConfig()
        self._sizer = sizer
        strict = self.config.mode == DecisionMode.QINGPAI_STRICT
        self._policy = EntryPolicy(
            EntryPolicyConfig(
                policy_id=self.config.policy_id,
                min_grade=self.config.min_grade,
                require_confirmed=self.config.require_confirmed if strict else False,
                accepted_bsp_types=self.config.accepted_bsp_types,
                long_only=not self.config.allow_short,
                reject_hard_blockers=strict,
                structure_stop_mode=("qingpai_strict" if strict else "legacy"),
            )
        )

    def evaluate(
        self,
        signal: StrategySignal,
        event: SignalEvent | None,
        assessment: SignalAssessment | None,
        *,
        account_equity: float | None = None,
        available_funds: float | None = None,
        atr: float | None = None,
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
        if invariant_failures:
            return replace(
                decision,
                accepted=False,
                reason_codes=decision.reason_codes + tuple(invariant_failures),
                entry_price_hint=None,
                invalidation_price=None,
                initial_stop_price=None,
                position_size_hint=None,
                setup_invalidation_price=None,
                execution_stop_price=None,
            )
        return self._size_decision(
            decision,
            signal=signal,
            account_equity=account_equity,
            available_funds=available_funds,
            atr=atr,
        )

    def _size_decision(
        self,
        decision: SignalDecision,
        *,
        signal: StrategySignal,
        account_equity: float | None,
        available_funds: float | None,
        atr: float | None,
    ) -> SignalDecision:
        if not decision.accepted or signal.target_position == 0 or self._sizer is None:
            return decision

        stop = decision.execution_stop_price
        if stop is None:
            stop = decision.initial_stop_price
        entry = float(signal.price)
        if stop is None:
            return replace(
                decision,
                accepted=False,
                reason_codes=decision.reason_codes + ("execution_stop_unavailable",),
                entry_price_hint=None,
                position_size_hint=None,
            )

        if self.config.mode == DecisionMode.QINGPAI_STRICT:
            setup = decision.setup_invalidation_price
            wrong_side: list[str] = []
            if not _is_protective_side(signal.target_position, entry, float(stop)):
                wrong_side.append("execution_stop_wrong_side_at_entry")
            if setup is not None and not _is_protective_side(
                signal.target_position, entry, float(setup)
            ):
                wrong_side.append("setup_invalidation_wrong_side_at_entry")
            if wrong_side:
                return replace(
                    decision,
                    accepted=False,
                    reason_codes=decision.reason_codes + tuple(wrong_side),
                    entry_price_hint=None,
                    position_size_hint=None,
                )

        lots = self._sizer.calculate(
            initial_stop_price=float(stop),
            entry_price=entry,
            atr=atr,
            account_equity=account_equity,
        )
        if self.config.max_abs_position is not None:
            lots = min(lots, self.config.max_abs_position)
        margin_cap_applied = False
        if self.config.margin_rate > 0 and available_funds is not None:
            margin_per_lot = (
                entry * self.config.contract_multiplier * self.config.margin_rate
            )
            margin_budget = max(0.0, float(available_funds)) * (
                self.config.max_margin_utilization
            )
            margin_lots = floor(margin_budget / margin_per_lot)
            margin_cap_applied = margin_lots < lots
            lots = min(lots, margin_lots)
        if lots < 1:
            reason = (
                "margin_budget_below_one_lot"
                if self.config.margin_rate > 0 and available_funds is not None
                else "risk_budget_below_one_lot"
            )
            return replace(
                decision,
                accepted=False,
                reason_codes=decision.reason_codes + (reason,),
                entry_price_hint=None,
                position_size_hint=0.0,
            )
        reason_codes = decision.reason_codes
        if margin_cap_applied:
            reason_codes = reason_codes + ("margin_cap_applied",)
        return replace(
            decision,
            reason_codes=reason_codes,
            entry_price_hint=entry,
            position_size_hint=float(lots),
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
        setup_invalidation_price=None,
        execution_stop_price=None,
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


def _is_protective_side(
    target_position: int,
    entry_price: float,
    stop_price: float,
) -> bool:
    if target_position > 0:
        return stop_price < entry_price
    return stop_price > entry_price
