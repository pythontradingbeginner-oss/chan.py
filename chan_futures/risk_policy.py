"""OPEN-005 risk-gate modes and fixed research profiles."""

from __future__ import annotations

from enum import Enum
from typing import Mapping


class RiskGateMode(str, Enum):
    ENFORCE = "enforce"
    OBSERVE = "observe"
    DISABLED = "disabled"


class RiskProfile(str, Enum):
    CURRENT_ENFORCED = "CURRENT_ENFORCED"
    B0_UNINTERRUPTED_STRATEGY_PATH = "B0_UNINTERRUPTED_STRATEGY_PATH"
    B1_CONSECUTIVE_LOSS_ABLATION = "B1_CONSECUTIVE_LOSS_ABLATION"
    D_EXPERIMENT = "D_EXPERIMENT"


STATE_GATE_ORDER = (
    "max_loss_points",
    "daily_loss_limit",
    "max_consecutive_losses",
    "max_drawdown_pct",
)


PROFILE_GATE_MODES: dict[RiskProfile, dict[str, RiskGateMode]] = {
    RiskProfile.CURRENT_ENFORCED: {
        gate: RiskGateMode.ENFORCE for gate in STATE_GATE_ORDER
    },
    RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH: {
        gate: RiskGateMode.OBSERVE for gate in STATE_GATE_ORDER
    },
    RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION: {
        "max_loss_points": RiskGateMode.ENFORCE,
        "daily_loss_limit": RiskGateMode.ENFORCE,
        "max_consecutive_losses": RiskGateMode.DISABLED,
        "max_drawdown_pct": RiskGateMode.ENFORCE,
    },
    RiskProfile.D_EXPERIMENT: {
        "max_loss_points": RiskGateMode.ENFORCE,
        "daily_loss_limit": RiskGateMode.ENFORCE,
        "max_consecutive_losses": RiskGateMode.DISABLED,
        "max_drawdown_pct": RiskGateMode.ENFORCE,
    },
}


def normalize_risk_profile(value: object) -> RiskProfile:
    if isinstance(value, RiskProfile):
        return value
    text = str(value or RiskProfile.CURRENT_ENFORCED.value).strip().upper()
    aliases = {
        "CURRENT": RiskProfile.CURRENT_ENFORCED,
        "CURRENT_ENFORCED": RiskProfile.CURRENT_ENFORCED,
        "B0": RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH,
        "B0_UNINTERRUPTED_STRATEGY_PATH": (
            RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH
        ),
        "B1": RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION,
        "B1_CONSECUTIVE_LOSS_ABLATION": (
            RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION
        ),
        "D": RiskProfile.D_EXPERIMENT,
        "D_EXPERIMENT": RiskProfile.D_EXPERIMENT,
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(f"unsupported risk profile: {value!r}") from exc


def normalize_gate_mode(value: object) -> RiskGateMode:
    if isinstance(value, RiskGateMode):
        return value
    try:
        return RiskGateMode(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported risk gate mode: {value!r}") from exc


def resolve_gate_modes(
    profile: object,
    overrides: Mapping[str, object | None] | None = None,
) -> dict[str, RiskGateMode]:
    normalized_profile = normalize_risk_profile(profile)
    result = dict(PROFILE_GATE_MODES[normalized_profile])
    for gate, value in (overrides or {}).items():
        if gate not in STATE_GATE_ORDER:
            raise ValueError(f"unknown risk gate: {gate}")
        if value is not None:
            result[gate] = normalize_gate_mode(value)
    return result
