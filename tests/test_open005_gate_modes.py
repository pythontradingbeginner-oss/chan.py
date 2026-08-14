from __future__ import annotations

from types import SimpleNamespace

import pytest

from chan_futures.config import RiskParams
from chan_futures.execution import SimulatedExecutionEngine
from chan_futures.position_transition import decompose_position_transition
from chan_futures.risk import RiskConfig, RiskManager
from chan_futures.risk_policy import RiskGateMode, RiskProfile
from chan_futures.strategy import StrategySignal


def _signal(target: int) -> StrategySignal:
    return StrategySignal(
        timestamp="2026-08-13 10:00:00",
        action="open_long" if target > 0 else "close_long",
        target_position=target,
        price=100.0,
        reason="test",
        bsp_type="1",
        bsp_bi_idx=1,
        bsp_klu_idx=1,
    )


def _fully_breached(profile: RiskProfile) -> RiskManager:
    risk = RiskManager(
        RiskConfig(
            max_loss_points=10,
            daily_loss_limit=5,
            max_consecutive_losses=2,
            max_drawdown_pct=0.10,
            require_session_key=True,
            profile=profile,
        ),
        initial_equity=1000,
    )
    risk.advance_session("2026-08-13", observed_at="2026-08-13 09:00:00")
    risk.on_fill(pnl_points=-10, session_key="2026-08-13")
    risk.on_fill(pnl_points=-10, session_key="2026-08-13")
    return risk


def test_current_enforced_emits_every_breach_in_stable_order() -> None:
    decision = _fully_breached(RiskProfile.CURRENT_ENFORCED).approve(
        _signal(1), current_equity=800
    )

    assert not decision.approved
    assert decision.reason.startswith("max_loss_points")
    assert decision.hard_failures == ()
    assert tuple(value.split(":", 1)[0] for value in decision.enforced_breaches) == (
        "max_loss_points",
        "daily_loss_limit",
        "max_consecutive_losses",
        "max_drawdown",
    )
    assert decision.observed_breaches == ()
    assert decision.evaluated_gates == (
        "max_loss_points",
        "daily_loss_limit",
        "max_consecutive_losses",
        "max_drawdown_pct",
    )


def test_b0_observes_all_state_breaches_without_weakening_hard_prerequisites() -> None:
    risk = _fully_breached(RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH)
    decision = risk.approve(_signal(1), current_equity=800)

    assert decision.approved
    assert decision.reason == "approved"
    assert decision.enforced_breaches == ()
    assert len(decision.observed_breaches) == 4

    missing = RiskManager(
        RiskConfig(
            daily_loss_limit=5,
            max_drawdown_pct=0.10,
            require_session_key=True,
            profile=RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH,
        ),
        initial_equity=1000,
    ).approve(_signal(1), current_equity=None)
    assert not missing.approved
    assert missing.hard_failures == (
        "risk_session_uninitialized",
        "account_equity_unavailable",
    )


def test_b1_disables_only_consecutive_loss_gate() -> None:
    risk = RiskManager(
        RiskConfig(
            max_loss_points=100,
            daily_loss_limit=100,
            max_consecutive_losses=2,
            max_drawdown_pct=0.10,
            profile=RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION,
        ),
        initial_equity=1000,
    )
    risk.advance_session("2026-08-13")
    risk.on_fill(pnl_points=-1, session_key="2026-08-13")
    risk.on_fill(pnl_points=-1, session_key="2026-08-13")

    decision = risk.approve(_signal(1), current_equity=1000)
    assert decision.approved
    assert "max_consecutive_losses" not in decision.evaluated_gates
    assert decision.enforced_breaches == ()
    assert decision.observed_breaches == ()
    assert decision.evaluated_gates == (
        "max_loss_points",
        "daily_loss_limit",
        "max_drawdown_pct",
    )


def test_risk_reducing_leg_bypasses_state_gates() -> None:
    decision = _fully_breached(RiskProfile.CURRENT_ENFORCED).approve(
        _signal(0),
        current_equity=None,
        before_position=1,
    )
    assert decision.approved
    assert decision.reason == "risk_reducing"
    assert decision.evaluated_gates == ()


@pytest.mark.parametrize(
    ("before", "target", "close", "open_"),
    [
        (0, 1, None, (0, 1, 1)),
        (2, 1, (2, 1, 1), None),
        (1, 2, None, (1, 2, 1)),
        (2, -1, (2, 0, 2), (0, -1, 1)),
        (-2, 1, (-2, 0, 2), (0, 1, 1)),
    ],
)
def test_position_transition_decomposition(before, target, close, open_) -> None:
    transition = decompose_position_transition(before, target)
    actual_close = (
        None
        if transition.close_leg is None
        else (
            transition.close_leg.before_position,
            transition.close_leg.target_position,
            transition.close_leg.quantity,
        )
    )
    actual_open = (
        None
        if transition.open_leg is None
        else (
            transition.open_leg.before_position,
            transition.open_leg.target_position,
            transition.open_leg.quantity,
        )
    )
    assert actual_close == close
    assert actual_open == open_


def test_risk_params_resolve_fixed_profiles_and_explicit_overrides() -> None:
    b0 = RiskParams.from_dict({"profile": "B0"})
    assert set(b0.gate_modes().values()) == {RiskGateMode.OBSERVE}

    overridden = RiskParams.from_dict(
        {"profile": "B0", "max_loss_points_mode": "enforce"}
    )
    assert overridden.gate_modes()["max_loss_points"] == RiskGateMode.ENFORCE
    assert overridden.gate_modes()["daily_loss_limit"] == RiskGateMode.OBSERVE

    with pytest.raises(ValueError, match="unsupported risk profile"):
        RiskParams.from_dict({"profile": "UNKNOWN"})
    with pytest.raises(ValueError, match="unsupported risk gate mode"):
        RiskParams.from_dict({"max_loss_points_mode": "silent"})


def test_reversal_open_leg_is_approved_from_flat_against_its_own_target() -> None:
    risk = RiskManager(RiskConfig(max_abs_position=1))
    reverse = SimpleNamespace(**{**_signal(-1).__dict__, "action": "reverse_long_to_short"})

    approved = risk.approve(reverse, before_position=2)

    assert approved.approved
    oversized = SimpleNamespace(**{**reverse.__dict__, "target_position": -2})
    rejected = risk.approve(oversized, before_position=1)
    assert not rejected.approved
    assert rejected.hard_failures == ("max_abs_position",)


def test_simulated_execution_handles_same_sign_reduce_and_add() -> None:
    engine = SimulatedExecutionEngine(fee_points=0, slippage_points=0)
    engine.execute(_signal(2))
    engine.execute(SimpleNamespace(**{**_signal(1).__dict__, "price": 110.0}))
    assert engine.state.position == 1
    assert engine.state.avg_price == 100.0
    assert engine.state.realized_points == 10.0

    engine.execute(SimpleNamespace(**{**_signal(2).__dict__, "price": 120.0}))
    assert engine.state.position == 2
    assert engine.state.avg_price == 110.0
