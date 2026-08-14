"""Frozen OPEN-005 Option D probe-regime state machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

import pandas as pd


class OptionDRegime(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED_UNTIL_NEXT_SESSION = "PAUSED_UNTIL_NEXT_SESSION"
    PROBE_AVAILABLE = "PROBE_AVAILABLE"
    PROBE_IN_FLIGHT = "PROBE_IN_FLIGHT"


@dataclass(slots=True)
class ReservedProbeIntent:
    intent_id: str
    decision_id: str
    event_id: str
    target_position: int
    planned_volume: int
    intent_snapshot: dict
    probe_epoch_id: str
    filled_volume: int = 0
    order_ids: list[str] = field(default_factory=list)
    terminal_order_ids: list[str] = field(default_factory=list)
    reported_fill_order_ids: list[str] = field(default_factory=list)

    def to_state(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "decision_id": self.decision_id,
            "event_id": self.event_id,
            "target_position": self.target_position,
            "planned_volume": self.planned_volume,
            "filled_volume": self.filled_volume,
            "order_ids": list(self.order_ids),
            "terminal_order_ids": list(self.terminal_order_ids),
            "reported_fill_order_ids": list(self.reported_fill_order_ids),
            "probe_epoch_id": self.probe_epoch_id,
            "intent_snapshot": dict(self.intent_snapshot),
        }

    @classmethod
    def from_state(cls, state: dict) -> "ReservedProbeIntent":
        return cls(
            intent_id=_identity(state.get("intent_id"), "intent_id"),
            decision_id=_identity(state.get("decision_id"), "decision_id"),
            event_id=_identity(state.get("event_id"), "event_id"),
            target_position=int(state.get("target_position", 0)),
            planned_volume=int(state.get("planned_volume", 0)),
            filled_volume=int(state.get("filled_volume", 0)),
            order_ids=[str(value) for value in state.get("order_ids", [])],
            terminal_order_ids=[
                str(value) for value in state.get("terminal_order_ids", [])
            ],
            reported_fill_order_ids=[
                str(value) for value in state.get("reported_fill_order_ids", [])
            ],
            probe_epoch_id=_identity(
                state.get("probe_epoch_id"), "probe_epoch_id"
            ),
            intent_snapshot=dict(state.get("intent_snapshot") or {}),
        )


@dataclass(slots=True)
class OptionDState:
    regime_state: OptionDRegime = OptionDRegime.ACTIVE
    paused_at_session: str | None = None
    eligible_from_session: str | None = None
    probe_epoch_id: str | None = None
    probe_order_ids: list[str] = field(default_factory=list)
    probe_position_id: str | None = None
    reserved_intent_id: str | None = None
    epoch_sequence: int = 0

    def to_state(self) -> dict:
        return {
            "regime_state": self.regime_state.value,
            "paused_at_session": self.paused_at_session,
            "eligible_from_session": self.eligible_from_session,
            "probe_epoch_id": self.probe_epoch_id,
            "probe_order_ids": list(self.probe_order_ids),
            "probe_position_id": self.probe_position_id,
            "reserved_intent_id": self.reserved_intent_id,
            "epoch_sequence": self.epoch_sequence,
        }

    @classmethod
    def from_state(cls, state: dict) -> "OptionDState":
        return cls(
            regime_state=OptionDRegime(
                state.get("regime_state", OptionDRegime.ACTIVE.value)
            ),
            paused_at_session=_session_or_none(state.get("paused_at_session")),
            eligible_from_session=_session_or_none(
                state.get("eligible_from_session")
            ),
            probe_epoch_id=_text_or_none(state.get("probe_epoch_id")),
            probe_order_ids=[str(value) for value in state.get("probe_order_ids", [])],
            probe_position_id=_text_or_none(state.get("probe_position_id")),
            reserved_intent_id=_text_or_none(state.get("reserved_intent_id")),
            epoch_sequence=int(state.get("epoch_sequence", 0) or 0),
        )


class OptionDController:
    """One-probe recovery policy driven only by finalized positions and sessions."""

    def __init__(
        self,
        *,
        enabled: bool,
        loss_threshold: int | None,
        next_session: Callable[[object], object] | None,
    ) -> None:
        if enabled and (loss_threshold is None or int(loss_threshold) < 1):
            raise ValueError("option_d requires max_consecutive_losses >= 1")
        self.enabled = bool(enabled)
        self.loss_threshold = (
            int(loss_threshold) if loss_threshold is not None else None
        )
        self._next_session = next_session
        self.state = OptionDState()
        self.reserved_intents: dict[str, ReservedProbeIntent] = {}

    def entry_block_reason(self) -> str | None:
        if not self.enabled or self.state.regime_state == OptionDRegime.ACTIVE:
            return None
        if self.state.regime_state == OptionDRegime.PAUSED_UNTIL_NEXT_SESSION:
            return (
                "option_d_paused_until_next_session: eligible_from="
                f"{self.state.eligible_from_session}"
            )
        if self.state.regime_state == OptionDRegime.PROBE_IN_FLIGHT:
            return "option_d_probe_in_flight"
        if self.state.reserved_intent_id is not None:
            return "option_d_probe_reserved"
        return None

    @property
    def probe_available(self) -> bool:
        return (
            self.enabled
            and self.state.regime_state == OptionDRegime.PROBE_AVAILABLE
            and self.state.reserved_intent_id is None
        )

    def advance_session(self, session_key: object) -> bool:
        if not self.enabled:
            return False
        candidate = _session(session_key)
        if self.state.regime_state != OptionDRegime.PAUSED_UNTIL_NEXT_SESSION:
            return False
        eligible = self.state.eligible_from_session
        if eligible is None or pd.Timestamp(candidate) < pd.Timestamp(eligible):
            return False
        self.state.epoch_sequence += 1
        self.state.regime_state = OptionDRegime.PROBE_AVAILABLE
        self.state.probe_epoch_id = _epoch_id(
            paused_at=self.state.paused_at_session or candidate,
            eligible_from=eligible,
            sequence=self.state.epoch_sequence,
        )
        self.state.probe_order_ids = []
        self.state.probe_position_id = None
        self.state.reserved_intent_id = None
        return True

    def on_position_finalized(
        self,
        *,
        position_id: str,
        result: str,
        session_key: object | None,
        consecutive_losses: int,
    ) -> None:
        if not self.enabled:
            return
        normalized_position = _identity(position_id, "position_id")
        is_probe = (
            self.state.regime_state == OptionDRegime.PROBE_IN_FLIGHT
            and self.state.probe_position_id == normalized_position
        )
        if is_probe:
            if result == "win":
                self._activate()
            else:
                self._pause(session_key)
            return
        if (
            self.state.regime_state == OptionDRegime.ACTIVE
            and result == "loss"
            and consecutive_losses >= int(self.loss_threshold or 0)
        ):
            self._pause(session_key)

    def reserve_probe_intent(
        self,
        *,
        intent_id: str,
        decision_id: str,
        event_id: str,
        target_position: int,
        planned_volume: int,
        intent_snapshot: dict,
    ) -> None:
        if not self.probe_available:
            raise ValueError("option_d_probe_not_available")
        normalized_id = _identity(intent_id, "intent_id")
        if int(planned_volume) < 1:
            raise ValueError("planned_volume must be >= 1")
        if (
            intent_snapshot.get("schema_version") != "trade-intent-runtime-v1"
            or not isinstance(intent_snapshot.get("signal"), dict)
            or not isinstance(intent_snapshot.get("decision"), dict)
        ):
            raise ValueError("option_d_intent_snapshot_incomplete")
        epoch_id = _identity(self.state.probe_epoch_id, "probe_epoch_id")
        record = ReservedProbeIntent(
            intent_id=normalized_id,
            decision_id=_identity(decision_id, "decision_id"),
            event_id=_identity(event_id, "event_id"),
            target_position=int(target_position),
            planned_volume=int(planned_volume),
            probe_epoch_id=epoch_id,
            intent_snapshot=dict(intent_snapshot),
        )
        self.reserved_intents[normalized_id] = record
        self.state.reserved_intent_id = normalized_id

    def bind_probe_orders(self, intent_id: str, order_ids: list[str]) -> None:
        record = self._reserved(intent_id)
        normalized = [_identity(value, "order_id") for value in order_ids]
        for order_id in normalized:
            if order_id not in record.order_ids:
                record.order_ids.append(order_id)
            if order_id not in self.state.probe_order_ids:
                self.state.probe_order_ids.append(order_id)

    def record_probe_open_fill(
        self,
        *,
        intent_id: str,
        position_id: str,
        fill_volume: int,
        order_id: str,
    ) -> None:
        record = self._reserved(intent_id)
        normalized_position = _identity(position_id, "position_id")
        normalized_order = _identity(order_id, "order_id")
        if int(fill_volume) < 1:
            raise ValueError("fill_volume must be >= 1")
        if normalized_order not in record.order_ids:
            self.bind_probe_orders(intent_id, [normalized_order])
        if self.state.regime_state == OptionDRegime.PROBE_AVAILABLE:
            self.state.regime_state = OptionDRegime.PROBE_IN_FLIGHT
            self.state.probe_position_id = normalized_position
        elif (
            self.state.regime_state != OptionDRegime.PROBE_IN_FLIGHT
            or self.state.probe_position_id != normalized_position
        ):
            raise ValueError("option_d_probe_position_mismatch")
        record.filled_volume = min(
            record.planned_volume,
            record.filled_volume + int(fill_volume),
        )

    def mark_probe_order_terminal(
        self,
        order_id: str,
        *,
        has_reported_fill: bool = False,
    ) -> bool:
        normalized_order = _identity(order_id, "order_id")
        intent_id = self.state.reserved_intent_id
        if intent_id is None:
            return False
        record = self.reserved_intents.get(intent_id)
        if record is None or normalized_order not in record.order_ids:
            return False
        if normalized_order not in record.terminal_order_ids:
            record.terminal_order_ids.append(normalized_order)
        if (
            has_reported_fill
            and normalized_order not in record.reported_fill_order_ids
        ):
            record.reported_fill_order_ids.append(normalized_order)
        all_terminal = bool(record.order_ids) and set(record.order_ids).issubset(
            record.terminal_order_ids
        )
        if (
            all_terminal
            and record.filled_volume == 0
            and not record.reported_fill_order_ids
            and self.state.regime_state == OptionDRegime.PROBE_AVAILABLE
        ):
            self.release_probe_reservation(intent_id)
        return True

    def release_probe_reservation(self, intent_id: str) -> None:
        record = self._reserved(intent_id)
        if record.filled_volume != 0:
            raise ValueError("option_d_filled_probe_cannot_be_released")
        self.reserved_intents.pop(record.intent_id, None)
        self.state.reserved_intent_id = None
        self.state.probe_order_ids = []

    def current_reserved_intent(self) -> dict | None:
        intent_id = self.state.reserved_intent_id
        if intent_id is None:
            return None
        record = self.reserved_intents.get(intent_id)
        return record.to_state() if record is not None else None

    def to_state(self) -> tuple[dict, dict[str, dict]]:
        return (
            self.state.to_state(),
            {
                key: value.to_state()
                for key, value in sorted(self.reserved_intents.items())
            },
        )

    def load_state(self, option_d: dict, reserved_intents: dict) -> None:
        if not self.enabled:
            if option_d or reserved_intents:
                raise ValueError("option_d_state_requires_d_experiment_profile")
            return
        self.state = OptionDState.from_state(option_d or {})
        self.reserved_intents = {
            str(intent_id): ReservedProbeIntent.from_state(dict(value or {}))
            for intent_id, value in (reserved_intents or {}).items()
        }
        self._validate()

    def _pause(self, session_key: object | None) -> None:
        if session_key is None:
            raise ValueError("option_d_session_required_for_pause")
        paused = _session(session_key)
        if self._next_session is None:
            raise ValueError("option_d_next_session_resolver_unavailable")
        eligible = _session(self._next_session(paused))
        self.state.regime_state = OptionDRegime.PAUSED_UNTIL_NEXT_SESSION
        self.state.paused_at_session = paused
        self.state.eligible_from_session = eligible
        self.state.probe_epoch_id = None
        self.state.probe_order_ids = []
        self.state.probe_position_id = None
        self.state.reserved_intent_id = None
        self.reserved_intents.clear()

    def _activate(self) -> None:
        self.state.regime_state = OptionDRegime.ACTIVE
        self.state.paused_at_session = None
        self.state.eligible_from_session = None
        self.state.probe_epoch_id = None
        self.state.probe_order_ids = []
        self.state.probe_position_id = None
        self.state.reserved_intent_id = None
        self.reserved_intents.clear()

    def _reserved(self, intent_id: str) -> ReservedProbeIntent:
        normalized = _identity(intent_id, "intent_id")
        if self.state.reserved_intent_id != normalized:
            raise ValueError("option_d_reserved_intent_mismatch")
        try:
            return self.reserved_intents[normalized]
        except KeyError as exc:
            raise ValueError("option_d_reserved_intent_missing") from exc

    def _validate(self) -> None:
        state = self.state
        intent_id = state.reserved_intent_id
        if intent_id is not None and intent_id not in self.reserved_intents:
            raise ValueError("option_d_reserved_intent_missing")
        if set(self.reserved_intents) - ({intent_id} if intent_id else set()):
            raise ValueError("option_d_orphan_reserved_intent")
        if state.regime_state == OptionDRegime.ACTIVE:
            if any(
                value is not None
                for value in (
                    state.paused_at_session,
                    state.eligible_from_session,
                    state.probe_epoch_id,
                    state.probe_position_id,
                    intent_id,
                )
            ) or state.probe_order_ids:
                raise ValueError("option_d_active_state_not_clean")
        if state.regime_state == OptionDRegime.PAUSED_UNTIL_NEXT_SESSION:
            if state.paused_at_session is None or state.eligible_from_session is None:
                raise ValueError("option_d_pause_session_missing")
            if (
                state.probe_epoch_id is not None
                or state.probe_position_id is not None
                or intent_id is not None
                or state.probe_order_ids
            ):
                raise ValueError("option_d_paused_state_has_probe")
        if state.regime_state == OptionDRegime.PROBE_AVAILABLE:
            if state.probe_epoch_id is None or state.probe_position_id is not None:
                raise ValueError("option_d_probe_available_identity_invalid")
        if state.regime_state == OptionDRegime.PROBE_IN_FLIGHT:
            if state.probe_position_id is None or intent_id is None:
                raise ValueError("option_d_probe_in_flight_identity_missing")
        if intent_id is not None:
            record = self.reserved_intents[intent_id]
            if record.probe_epoch_id != state.probe_epoch_id:
                raise ValueError("option_d_reserved_epoch_mismatch")
            if record.planned_volume < 1 or not 0 <= record.filled_volume <= record.planned_volume:
                raise ValueError("option_d_reserved_volume_invalid")
            if not set(record.terminal_order_ids).issubset(record.order_ids):
                raise ValueError("option_d_terminal_order_unknown")
            if not set(record.reported_fill_order_ids).issubset(record.order_ids):
                raise ValueError("option_d_reported_fill_order_unknown")
            if set(record.order_ids) != set(state.probe_order_ids):
                raise ValueError("option_d_probe_order_ids_mismatch")
            if (
                state.regime_state == OptionDRegime.PROBE_AVAILABLE
                and record.filled_volume != 0
            ):
                raise ValueError("option_d_available_probe_has_fill")
            if (
                state.regime_state == OptionDRegime.PROBE_IN_FLIGHT
                and record.filled_volume < 1
            ):
                raise ValueError("option_d_in_flight_probe_has_no_fill")


def _epoch_id(*, paused_at: str, eligible_from: str, sequence: int) -> str:
    payload = f"option-d-probe-v1|{paused_at}|{eligible_from}|{sequence}"
    return "probe_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def probe_intent_identity(probe_epoch_id: object, event_id: object) -> str:
    epoch = _identity(probe_epoch_id, "probe_epoch_id")
    event = _identity(event_id, "event_id")
    payload = f"option-d-intent-v1|{epoch}|{event}"
    return "probe_intent_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _session(value: object) -> str:
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception as exc:
        raise ValueError(f"invalid option_d session: {value!r}") from exc


def _session_or_none(value: object | None) -> str | None:
    return None if value in (None, "") else _session(value)


def _text_or_none(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _identity(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text
