"""Canonical decomposition of target-position changes into ordered legs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PositionLegKind(str, Enum):
    CLOSE = "close"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class PositionTransitionLeg:
    kind: PositionLegKind
    before_position: int
    target_position: int
    quantity: int

    @property
    def signed_quantity(self) -> int:
        return self.target_position - self.before_position


@dataclass(frozen=True, slots=True)
class PositionTransition:
    before_position: int
    target_position: int
    close_leg: PositionTransitionLeg | None = None
    open_leg: PositionTransitionLeg | None = None

    @property
    def is_noop(self) -> bool:
        return self.close_leg is None and self.open_leg is None

    @property
    def is_reversal(self) -> bool:
        return self.close_leg is not None and self.open_leg is not None

    @property
    def increases_exposure(self) -> bool:
        return self.open_leg is not None


def decompose_position_transition(
    before_position: int,
    target_position: int,
) -> PositionTransition:
    before = _position_value(before_position, "before_position")
    target = _position_value(target_position, "target_position")
    if before == target:
        return PositionTransition(before, target)

    before_sign = _sign(before)
    target_sign = _sign(target)
    if before == 0:
        return PositionTransition(
            before,
            target,
            open_leg=PositionTransitionLeg(
                PositionLegKind.OPEN, 0, target, abs(target)
            ),
        )
    if target == 0:
        return PositionTransition(
            before,
            target,
            close_leg=PositionTransitionLeg(
                PositionLegKind.CLOSE, before, 0, abs(before)
            ),
        )
    if before_sign != target_sign:
        return PositionTransition(
            before,
            target,
            close_leg=PositionTransitionLeg(
                PositionLegKind.CLOSE, before, 0, abs(before)
            ),
            open_leg=PositionTransitionLeg(
                PositionLegKind.OPEN, 0, target, abs(target)
            ),
        )
    if abs(target) < abs(before):
        return PositionTransition(
            before,
            target,
            close_leg=PositionTransitionLeg(
                PositionLegKind.CLOSE,
                before,
                target,
                abs(before) - abs(target),
            ),
        )
    return PositionTransition(
        before,
        target,
        open_leg=PositionTransitionLeg(
            PositionLegKind.OPEN,
            before,
            target,
            abs(target) - abs(before),
        ),
    )


def _position_value(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer position")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer position") from exc
    if normalized != value:
        raise ValueError(f"{field_name} must be an integer position")
    return normalized


def _sign(value: int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
