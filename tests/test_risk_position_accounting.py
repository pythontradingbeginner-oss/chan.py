"""OPEN005 close-fill accounting and position-finalization contract tests."""

from __future__ import annotations

import pytest

from chan_futures.backtest import _backtest_close_identity_fields
from chan_futures.risk import CloseFillRecord, RiskConfig, RiskManager
from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext, position_id_from_open_fill_id


def _fill(
    fill_id: str,
    *,
    position_id: str = "position-1",
    pnl: float,
    session: str = "2026-08-12",
) -> CloseFillRecord:
    return CloseFillRecord(
        fill_id=fill_id,
        order_id="order-1",
        position_id=position_id,
        pnl_raw_points=pnl,
        risk_session_key=session,
        observed_at=f"{session} 10:00:00",
        source="test_fill",
    )


def test_position_identity_is_stable_across_random_decision_ids() -> None:
    common = {
        "direction": SignalDirection.LONG,
        "entry_price": 3500.2,
        "entry_time": "2026-08-13 09:15:00",
        "entry_bar": 42,
        "volume": 1,
        "event_id": "event-stable",
        "signal_key": "signal-stable",
        "setup_candidate_id": "setup-stable",
        "active_symbol": "RB2610",
    }

    first = PositionContext(decision_id="random-a", **common)
    second = PositionContext(decision_id="random-b", **common)
    later = PositionContext(
        decision_id="random-c",
        **{**common, "entry_time": "2026-08-13 09:30:00"},
    )

    assert first.position_id == second.position_id
    assert first.position_id != later.position_id
    assert position_id_from_open_fill_id("CTP.TRADE-1") == (
        position_id_from_open_fill_id("CTP.TRADE-1")
    )
    assert position_id_from_open_fill_id("CTP.TRADE-1") != (
        position_id_from_open_fill_id("CTP.TRADE-2")
    )
    first_close = _backtest_close_identity_fields(first.position_id, 100)
    second_close = _backtest_close_identity_fields(second.position_id, 100)
    later_close = _backtest_close_identity_fields(later.position_id, 100)
    assert first_close == second_close
    assert first_close != later_close
    assert set(first_close) == {"position_id", "close_fill_id", "close_order_id"}


def test_duplicate_close_fill_is_a_noop() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100, require_session_key=True))
    fill = _fill("fill-1", pnl=-10.04)

    assert risk.record_close_fill(fill, remaining_quantity=1)
    state_after_first = risk.get_state()
    assert not risk.record_close_fill(fill, remaining_quantity=0)
    assert not risk.record_close_fill(fill, remaining_quantity=0)
    assert risk.get_state() == state_after_first


def test_partial_fills_update_fill_totals_but_finalize_streak_once() -> None:
    risk = RiskManager(RiskConfig(max_consecutive_losses=2))

    assert risk.record_close_fill(_fill("fill-1", pnl=-0.04), remaining_quantity=1)
    assert risk.record_close_fill(_fill("fill-2", pnl=-0.04), remaining_quantity=0)
    before = risk.get_state()
    assert before["realized_points"] == 0.0
    assert before["true_consecutive_losses"] == 0
    assert before["active_position_aggregators"]["position-1"] == {
        "position_id": "position-1",
        "raw_close_pnl_points": pytest.approx(-0.08),
        "normalized_close_pnl_points": 0.0,
        "close_fill_ids": ["fill-1", "fill-2"],
        "remaining_quantity": 0,
    }

    result = risk.finalize_position(
        "position-1",
        finalized_at="2026-08-12 10:01:00",
        reason="test_close",
    )
    assert result.pnl_raw_points == pytest.approx(-0.08)
    assert result.pnl_normalized_points == -0.1
    assert result.result == "loss"
    assert result.consecutive_losses == 1
    state = risk.get_state()
    assert state["true_consecutive_losses"] == 1
    assert state["finalized_position_results"]["position-1"] == {
        "position_id": "position-1",
        "raw_close_pnl_points": pytest.approx(-0.08),
        "normalized_position_pnl_points": -0.1,
        "result": "loss",
        "close_fill_ids": ["fill-1", "fill-2"],
        "finalized_at": "2026-08-12T10:01:00",
        "reason": "test_close",
    }


def test_position_result_uses_raw_aggregate_then_normalizes_once() -> None:
    risk = RiskManager()
    risk.record_close_fill(_fill("fill-1", pnl=0.04), remaining_quantity=1)
    risk.record_close_fill(_fill("fill-2", pnl=0.04), remaining_quantity=0)

    result = risk.finalize_position("position-1")

    assert risk.get_state()["realized_points"] == 0.0
    assert result.pnl_raw_points == pytest.approx(0.08)
    assert result.pnl_normalized_points == 0.1
    assert result.result == "win"


@pytest.mark.parametrize("pnl", [-0.04, 0.0, 0.04])
def test_position_result_rounding_band_is_flat(pnl: float) -> None:
    risk = RiskManager()
    risk.record_close_fill(_fill("fill-1", pnl=pnl), remaining_quantity=0)

    result = risk.finalize_position("position-1")

    assert result.pnl_normalized_points == 0.0
    assert result.result == "flat"


def test_fill_normalization_and_actual_fill_price_do_not_double_charge_slippage() -> None:
    position = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=100.0,
        entry_time="2026-08-12 09:00:00",
        entry_bar=1,
        volume=1,
        position_id="position-1",
    )
    # 98 is already the adverse actual fill; only the two one-point fees remain.
    pnl_raw = position.pnl_points(exit_price=98.0, fee_points=1.0)
    risk = RiskManager()
    risk.record_close_fill(_fill("fill-1", pnl=pnl_raw), remaining_quantity=0)
    result = risk.finalize_position("position-1")

    assert round(10.34, 1) == 10.3
    assert pnl_raw == -4.0
    assert result.pnl_raw_points == -4.0
    assert result.pnl_normalized_points == -4.0


def test_partial_closes_can_span_sessions_without_early_finalization() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100, require_session_key=True))
    risk.record_close_fill(
        _fill("fill-1", pnl=-7.0, session="2026-08-12"),
        remaining_quantity=1,
    )
    risk.record_close_fill(
        _fill("fill-2", pnl=2.0, session="2026-08-13"),
        remaining_quantity=0,
    )

    before = risk.get_state()
    assert before["realized_points"] == -5.0
    assert before["daily_realized"] == 2.0
    assert before["current_risk_session"] == "2026-08-13"
    assert before["true_consecutive_losses"] == 0

    result = risk.finalize_position("position-1")
    assert result.result == "loss"
    assert result.consecutive_losses == 1


def test_active_aggregator_and_idempotency_keys_survive_restore() -> None:
    original = RiskManager(
        RiskConfig(daily_loss_limit=100, require_session_key=True)
    )
    first = _fill("fill-1", pnl=-3.03)
    original.record_close_fill(first, remaining_quantity=1)

    restored = RiskManager(
        RiskConfig(daily_loss_limit=100, require_session_key=True)
    )
    restored.load_state(original.get_state())
    assert not restored.record_close_fill(first, remaining_quantity=1)
    assert restored.record_close_fill(
        _fill("fill-2", pnl=-1.02),
        remaining_quantity=0,
    )

    result = restored.finalize_position("position-1")
    assert result.pnl_raw_points == pytest.approx(-4.05)
    assert result.pnl_normalized_points == -4.0
    assert restored.get_state()["processed_close_fill_ids"] == ["fill-1", "fill-2"]

    reference = RiskManager(
        RiskConfig(daily_loss_limit=100, require_session_key=True)
    )
    reference.record_close_fill(first, remaining_quantity=1)
    reference.record_close_fill(_fill("fill-2", pnl=-1.02), remaining_quantity=0)
    reference_result = reference.finalize_position("position-1")
    assert result == reference_result
    for key in (
        "realized_points",
        "daily_realized",
        "true_consecutive_losses",
        "processed_close_fill_ids",
        "active_position_aggregators",
        "finalized_position_ids",
        "finalized_position_results",
    ):
        assert restored.get_state()[key] == reference.get_state()[key]


def test_finalize_requires_flat_position_and_is_idempotent() -> None:
    risk = RiskManager()
    risk.record_close_fill(_fill("fill-1", pnl=-1.0), remaining_quantity=1)
    with pytest.raises(ValueError, match="position_not_flat"):
        risk.finalize_position("position-1")

    risk.record_close_fill(_fill("fill-2", pnl=2.0), remaining_quantity=0)
    risk.finalize_position("position-1")
    state = risk.get_state()
    with pytest.raises(ValueError, match="position_already_finalized"):
        risk.finalize_position("position-1")
    assert risk.get_state() == state


def test_legacy_on_fill_remains_one_position_per_call() -> None:
    risk = RiskManager(RiskConfig(max_consecutive_losses=2))
    risk.on_fill(pnl_points=-2.04)
    risk.on_fill(pnl_points=-3.04)
    state = risk.get_state()

    assert state["realized_points"] == -5.0
    assert state["total_fills"] == 2
    assert state["true_consecutive_losses"] == 2
    assert state["active_position_aggregators"] == {}
    assert len(state["finalized_position_ids"]) == 2
