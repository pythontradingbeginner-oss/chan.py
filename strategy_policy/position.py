"""Canonical fill-derived position context shared by every runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from signal_core.models import (
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
)


@dataclass(frozen=True, slots=True)
class PositionContext:
    """Immutable holding snapshot created only after an opening fill."""

    direction: SignalDirection
    entry_price: float
    entry_time: object
    entry_bar: int | None
    volume: int

    position_id: str = ""
    entry_grade: str = "standard"
    event_id: str = ""
    signal_key: str = ""
    decision_id: str = ""
    policy_id: str = ""
    bsp_type: str = ""
    active_symbol: str | None = None

    bi_begin_price: float | None = None
    zs_high: float | None = None
    zs_low: float | None = None
    setup_invalidation_price: float | None = None
    execution_stop_price: float | None = None
    setup_candidate_schema_version: str = ""
    setup_candidate_id: str = ""
    setup_contract_epoch: str = ""
    setup_timeframe: str = ""
    setup_direction: str = ""
    setup_root_bi_idx: int | None = None
    setup_family: str = ""
    setup_state: str = "observed_only"

    def __post_init__(self) -> None:
        if self.volume < 1:
            raise ValueError("position volume must be >= 1")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if not self.position_id:
            object.__setattr__(self, "position_id", self._derived_position_id())

    def _derived_position_id(self) -> str:
        """Derive a replay-stable identity without the random decision UUID."""
        payload = "|".join(
            (
                "position-identity-v2",
                self.event_id,
                self.signal_key,
                self.setup_candidate_id,
                str(self.active_symbol or ""),
                _canonical_identity_time(self.entry_time),
                "" if self.entry_bar is None else str(self.entry_bar),
                float(self.entry_price).hex(),
                self.direction.value,
            )
        )
        return "position_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_decision_fill(
        cls,
        *,
        decision: SignalDecision,
        event: SignalEvent | None,
        assessment: SignalAssessment | None,
        target_position: int,
        fill_price: float,
        fill_volume: int,
        fill_time: object,
        entry_bar: int | None = None,
        bsp_type: str = "",
        active_symbol: str | None = None,
        position_id: str = "",
        setup_candidate_schema_version: str = "",
        setup_candidate_id: str = "",
        setup_contract_epoch: str = "",
        setup_timeframe: str = "",
        setup_direction: str = "",
        setup_root_bi_idx: int | None = None,
        setup_family: str = "",
        setup_state: str = "observed_only",
    ) -> PositionContext:
        if not decision.accepted:
            raise ValueError("rejected decision cannot create a position")
        if target_position == 0:
            raise ValueError("flat target cannot create a position")

        direction = (
            SignalDirection.LONG if target_position > 0 else SignalDirection.SHORT
        )
        if event is not None and event.direction != direction:
            raise ValueError("fill direction does not match SignalEvent")

        setup = decision.setup_invalidation_price
        if setup is None:
            setup = decision.invalidation_price
        execution_stop = decision.execution_stop_price
        if execution_stop is None:
            execution_stop = decision.initial_stop_price

        return cls(
            direction=direction,
            entry_price=float(fill_price),
            entry_time=fill_time,
            entry_bar=entry_bar,
            volume=int(fill_volume),
            position_id=position_id,
            entry_grade=(
                assessment.grade.value if assessment is not None else "standard"
            ),
            event_id=event.event_id if event is not None else decision.event_id,
            signal_key=event.signal_key if event is not None else "",
            decision_id=decision.decision_id,
            policy_id=decision.policy_id,
            bsp_type=bsp_type or (event.primary_bsp if event is not None else ""),
            active_symbol=active_symbol,
            bi_begin_price=event.bi_begin_price if event is not None else None,
            zs_high=event.zs_high if event is not None else None,
            zs_low=event.zs_low if event is not None else None,
            setup_invalidation_price=setup,
            execution_stop_price=execution_stop,
            setup_candidate_schema_version=setup_candidate_schema_version,
            setup_candidate_id=setup_candidate_id,
            setup_contract_epoch=setup_contract_epoch,
            setup_timeframe=setup_timeframe,
            setup_direction=setup_direction,
            setup_root_bi_idx=setup_root_bi_idx,
            setup_family=setup_family,
            setup_state=setup_state,
        )

    @classmethod
    def from_legacy_entry(
        cls,
        *,
        direction: SignalDirection,
        entry_price: float,
        bi_begin_price: float | None = None,
        zs_high: float | None = None,
        zs_low: float | None = None,
        initial_stop_price: float | None = None,
        invalidation_price: float | None = None,
        entry_grade: str | None = None,
    ) -> PositionContext:
        """Compatibility builder for callers that still use ExitManager.on_entry()."""
        return cls(
            direction=direction,
            entry_price=float(entry_price),
            entry_time=None,
            entry_bar=None,
            volume=1,
            entry_grade=entry_grade or "standard",
            bi_begin_price=bi_begin_price,
            zs_high=zs_high,
            zs_low=zs_low,
            setup_invalidation_price=invalidation_price,
            execution_stop_price=initial_stop_price,
        )

    def merge_open_fill(
        self,
        *,
        fill_price: float,
        fill_volume: int,
        fill_time: object | None = None,
    ) -> PositionContext:
        """Return a weighted position snapshot after another partial opening fill."""
        if fill_volume < 1:
            raise ValueError("fill_volume must be >= 1")
        total_volume = self.volume + int(fill_volume)
        weighted_price = (
            self.entry_price * self.volume + float(fill_price) * fill_volume
        ) / total_volume
        return PositionContext(
            direction=self.direction,
            entry_price=weighted_price,
            entry_time=self.entry_time if self.entry_time is not None else fill_time,
            entry_bar=self.entry_bar,
            volume=total_volume,
            position_id=self.position_id,
            entry_grade=self.entry_grade,
            event_id=self.event_id,
            signal_key=self.signal_key,
            decision_id=self.decision_id,
            policy_id=self.policy_id,
            bsp_type=self.bsp_type,
            active_symbol=self.active_symbol,
            bi_begin_price=self.bi_begin_price,
            zs_high=self.zs_high,
            zs_low=self.zs_low,
            setup_invalidation_price=self.setup_invalidation_price,
            execution_stop_price=self.execution_stop_price,
            setup_candidate_schema_version=self.setup_candidate_schema_version,
            setup_candidate_id=self.setup_candidate_id,
            setup_contract_epoch=self.setup_contract_epoch,
            setup_timeframe=self.setup_timeframe,
            setup_direction=self.setup_direction,
            setup_root_bi_idx=self.setup_root_bi_idx,
            setup_family=self.setup_family,
            setup_state=self.setup_state,
        )

    def pnl_points(
        self,
        *,
        exit_price: float,
        fee_points: float = 0.0,
        volume: int | None = None,
    ) -> float:
        lots = self.volume if volume is None else min(int(volume), self.volume)
        sign = 1 if self.direction == SignalDirection.LONG else -1
        return (
            sign * (float(exit_price) - self.entry_price) - float(fee_points) * 2
        ) * lots

    def reduce_volume(self, fill_volume: int) -> PositionContext | None:
        if fill_volume < 1:
            raise ValueError("fill_volume must be >= 1")
        remaining = self.volume - int(fill_volume)
        if remaining <= 0:
            return None
        return replace(self, volume=remaining)

    @property
    def initial_stop_price(self) -> float | None:
        return self.execution_stop_price

    @property
    def invalidation_price(self) -> float | None:
        return self.setup_invalidation_price


def position_id_from_open_fill_id(fill_id: str) -> str:
    """Create the runtime position identity from the first accepted open fill."""
    normalized = str(fill_id).strip()
    if not normalized:
        raise ValueError("open fill identity is required for position_id")
    payload = f"runtime-open-fill-v1|{normalized}"
    return "position_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_identity_time(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds")
