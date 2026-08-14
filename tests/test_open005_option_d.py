from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from chan_futures.option_d import OptionDRegime, probe_intent_identity
from chan_futures.risk import CloseFillRecord, RiskConfig, RiskManager
from chan_futures.risk_policy import RiskProfile
from chan_futures.strategy import StrategySignal
from chan_futures.trade_intent import PendingEntry, TradeIntent
from signal_core.models import SignalDecision
from vnpy.trader.constant import Direction, Offset, Status
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.live_engine import LiveTradingEngine
from vnpy_chan.order_state import CtaOrderStatusMachine


def _risk(threshold: int = 2) -> RiskManager:
    return RiskManager(
        RiskConfig(
            max_abs_position=1,
            max_consecutive_losses=threshold,
            profile=RiskProfile.D_EXPERIMENT,
        ),
        next_session_resolver=lambda value: {
            "2026-08-13": "2026-08-14",
            "2026-08-14": "2026-08-17",
        }[str(value)],
    )


def _signal(target: int = 1, *, event: str = "event-probe") -> StrategySignal:
    return StrategySignal(
        timestamp=datetime(2026, 8, 14, 9, 15),
        action="open_long" if target > 0 else "open_short",
        target_position=target,
        price=3500.0,
        reason=event,
        bsp_type="1",
        bsp_bi_idx=10,
        bsp_klu_idx=20,
        active_symbol="RB2610",
    )


def _intent(target: int = 1, *, event: str = "event-probe") -> TradeIntent:
    return TradeIntent(
        signal=_signal(target, event=event),
        decision=SignalDecision(
            decision_id=f"decision-{event}",
            event_id=event,
            policy_id="test-policy",
            accepted=True,
            reason_codes=(),
            entry_price_hint=3500.0,
            invalidation_price=3450.0,
            initial_stop_price=3450.0,
            position_size_hint=1.0,
            decided_at=datetime(2026, 8, 14, 9, 15),
            setup_invalidation_price=3450.0,
            execution_stop_price=3450.0,
        ),
    )


def _finalize(
    risk: RiskManager,
    position_id: str,
    pnl: float,
    *,
    session: str,
) -> None:
    risk.record_close_fill(
        CloseFillRecord(
            fill_id=f"fill-{position_id}",
            order_id=f"order-{position_id}",
            position_id=position_id,
            pnl_raw_points=pnl,
            risk_session_key=session,
            observed_at=f"{session} 14:45:00",
            source="test",
        ),
        remaining_quantity=0,
    )
    risk.finalize_position(position_id, finalized_at=f"{session} 14:45:00")


def _pause(risk: RiskManager) -> None:
    risk.advance_session("2026-08-13")
    _finalize(risk, "loss-1", -1, session="2026-08-13")
    _finalize(risk, "loss-2", -1, session="2026-08-13")


def _reserve(risk: RiskManager, intent: TradeIntent | None = None) -> tuple[str, TradeIntent]:
    intent = intent or _intent()
    epoch = risk.get_state()["option_d"]["probe_epoch_id"]
    intent_id = probe_intent_identity(epoch, intent.event_id)
    risk.reserve_probe_intent(
        intent_id=intent_id,
        decision_id=intent.decision.decision_id,
        event_id=intent.event_id,
        target_position=intent.signal.target_position,
        planned_volume=1,
        intent_snapshot=intent.to_runtime_snapshot(),
    )
    return intent_id, intent


def test_option_d_pauses_at_threshold_and_releases_only_next_session() -> None:
    risk = _risk()
    _pause(risk)

    state = risk.get_state()["option_d"]
    assert state["regime_state"] == OptionDRegime.PAUSED_UNTIL_NEXT_SESSION
    assert state["eligible_from_session"] == "2026-08-14"
    assert not risk.approve(_signal()).approved

    risk.advance_session("2026-08-14")
    state = risk.get_state()["option_d"]
    assert state["regime_state"] == OptionDRegime.PROBE_AVAILABLE
    assert risk.approve(_signal()).approved


def test_probe_snapshot_uses_canonical_open_leg_intent() -> None:
    source = _intent(-3)

    open_intent = source.for_open_leg(2)

    assert open_intent.signal.action == "open_short"
    assert open_intent.signal.target_position == -2
    assert open_intent.decision is source.decision
    with pytest.raises(ValueError, match="positive integer"):
        source.for_open_leg(0)


def test_probe_availability_persists_and_zero_fill_terminal_releases_reservation() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    risk.advance_session("2026-08-17")
    intent_id, _ = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])

    assert not risk.approve(_signal(event="second"), before_position=0).approved
    assert risk.mark_probe_order_terminal("OPEN.1")

    state = risk.get_state()["option_d"]
    assert state["regime_state"] == OptionDRegime.PROBE_AVAILABLE
    assert state["reserved_intent_id"] is None
    assert risk.approve(_signal(event="retry")).approved


def test_terminal_order_reported_fill_does_not_release_before_trade_event() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, _ = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])

    assert risk.mark_probe_order_terminal("OPEN.1", has_reported_fill=True)
    assert risk.get_state()["option_d"]["reserved_intent_id"] == intent_id
    assert risk.get_state()["reserved_intents"][intent_id][
        "reported_fill_order_ids"
    ] == ["OPEN.1"]

    risk.record_probe_open_fill(
        intent_id=intent_id,
        position_id="probe-position",
        fill_volume=1,
        order_id="OPEN.1",
    )
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_IN_FLIGHT"


def test_first_probe_fill_consumes_availability_and_survives_restart() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])
    risk.record_probe_open_fill(
        intent_id=intent_id,
        position_id="probe-position",
        fill_volume=1,
        order_id="OPEN.1",
    )

    restored = _risk()
    restored.load_state(risk.get_state())
    reserved = restored.current_reserved_probe_intent()
    restored_intent = TradeIntent.from_runtime_snapshot(reserved["intent_snapshot"])

    assert restored.get_state()["option_d"]["regime_state"] == "PROBE_IN_FLIGHT"
    assert restored.get_state()["option_d"]["probe_position_id"] == "probe-position"
    assert restored_intent.signal == intent.signal
    assert restored_intent.decision == intent.decision
    assert not restored.approve(_signal(event="other")).approved


def test_option_d_restore_rejects_legacy_or_incomplete_state() -> None:
    legacy = RiskManager(RiskConfig(max_consecutive_losses=2)).get_state()

    with pytest.raises(ValueError, match="option_d_runtime_state_version_invalid"):
        _risk().load_state(legacy)

    incomplete = _risk().get_state()
    incomplete.pop("reserved_intents")
    with pytest.raises(ValueError, match="option_d_runtime_state_incomplete"):
        _risk().load_state(incomplete)


def test_option_d_invalid_restore_is_atomic() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    _reserve(risk)
    before = deepcopy(risk.get_state())
    invalid = deepcopy(before)
    invalid["realized_points"] = 999.0
    invalid["option_d"]["regime_state"] = "PROBE_IN_FLIGHT"
    invalid["option_d"]["probe_position_id"] = None

    with pytest.raises(ValueError, match="option_d_probe_in_flight_identity_missing"):
        risk.load_state(invalid)

    assert risk.get_state() == before


@pytest.mark.parametrize(
    ("pnl", "expected"),
    [(5.0, "ACTIVE"), (-5.0, "PAUSED_UNTIL_NEXT_SESSION"), (0.0, "PAUSED_UNTIL_NEXT_SESSION")],
)
def test_probe_finalize_transitions_by_position_result(pnl: float, expected: str) -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, _ = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])
    risk.record_probe_open_fill(
        intent_id=intent_id,
        position_id="probe-position",
        fill_volume=1,
        order_id="OPEN.1",
    )

    _finalize(risk, "probe-position", pnl, session="2026-08-14")

    assert risk.get_state()["option_d"]["regime_state"] == expected
    if pnl > 0:
        assert risk.get_state()["true_consecutive_losses"] == 0
    elif pnl < 0:
        assert risk.get_state()["true_consecutive_losses"] == 3
    else:
        assert risk.get_state()["true_consecutive_losses"] == 2


def _cta_probe_reversal(pnl_price: float) -> tuple[ChanBspStrategy, list[object]]:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, reverse_intent = _reserve(risk, _intent(-1, event="reverse"))
    risk.bind_probe_orders(intent_id, ["OPEN.PROBE"])
    risk.record_probe_open_fill(
        intent_id=intent_id,
        position_id="probe-position",
        fill_volume=1,
        order_id="OPEN.PROBE",
    )

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(["CLOSE.1"], role="close", price=pnl_price, volume=1)
    strategy._risk = risk
    strategy._position_context = reverse_intent.position_from_fill(
        fill_price=100.0,
        fill_volume=1,
        fill_time=datetime(2026, 8, 14, 9, 0),
        position_id="probe-position",
    )
    strategy._config = SimpleNamespace(
        execution=SimpleNamespace(fee_points=0.0),
        risk=SimpleNamespace(max_drawdown_pct=None),
    )
    strategy._risk_session_resolver = SimpleNamespace(
        resolve=lambda observed_at, source: SimpleNamespace(
            session_key="2026-08-14",
            observed_at=observed_at,
            source=source,
        )
    )
    strategy._account_equity_observation = lambda: (None, "test", "test")
    strategy._exit_manager = SimpleNamespace(on_close=lambda: None)
    strategy._pending_entry = None
    strategy._pending_reversal = PendingEntry(reverse_intent)
    strategy.total_pnl = 0.0
    strategy.total_trades = 0
    strategy._trade_pnl_batch = []
    strategy.exit_reason = "strategy_reverse"
    strategy.put_event = lambda: None
    strategy.write_log = lambda message: None
    events: list[object] = []
    strategy.trading = True
    strategy._persist_runtime_state = lambda: events.append("persist")
    strategy.sync_data = lambda: events.append("sync")
    strategy._approve_and_submit_open_intent = (
        lambda intent, before_position: events.append(
            ("open", risk.get_state()["option_d"]["regime_state"])
        )
    )
    return strategy, events


def test_probe_loss_reversal_finalizes_before_open_and_open_is_cancelled() -> None:
    strategy, events = _cta_probe_reversal(101.0)

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.CLOSE,
            vt_tradeid="CLOSE.FILL",
            vt_orderid="CLOSE.1",
            volume=1,
            price=101.0,
            datetime=datetime(2026, 8, 14, 10, 0),
        )
    )

    assert strategy._pending_reversal is None
    assert strategy._risk.get_state()["option_d"]["regime_state"] == (
        "PAUSED_UNTIL_NEXT_SESSION"
    )
    assert not any(isinstance(value, tuple) and value[0] == "open" for value in events)


def test_probe_win_reversal_reapproves_after_active_transition() -> None:
    strategy, events = _cta_probe_reversal(99.0)

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.CLOSE,
            vt_tradeid="CLOSE.FILL",
            vt_orderid="CLOSE.1",
            volume=1,
            price=99.0,
            datetime=datetime(2026, 8, 14, 10, 0),
        )
    )

    assert strategy._risk.get_state()["option_d"]["regime_state"] == "ACTIVE"
    assert ("open", "ACTIVE") in events


def test_cta_partial_probe_order_terminal_clears_pending_entry_but_keeps_in_flight() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])
    risk.record_probe_open_fill(
        intent_id=intent_id,
        position_id="probe-position",
        fill_volume=1,
        order_id="OPEN.1",
    )
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(["OPEN.1"], role="open", price=3500, volume=2)
    strategy._pending_entry = PendingEntry(
        intent,
        filled_volume=1,
        probe_intent_id=intent_id,
    )
    strategy._risk = risk
    strategy.order_status = "pending_open"
    strategy.put_event = lambda: None
    strategy._sync_runtime_state_checkpoint = lambda: None

    strategy.on_order(
        SimpleNamespace(
            vt_orderid="OPEN.1",
            price=3500,
            volume=2,
            traded=1,
            status=SimpleNamespace(name="CANCELLED"),
            is_active=lambda: False,
        )
    )

    assert strategy._pending_entry is None
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_IN_FLIGHT"


@pytest.mark.parametrize("terminal_status", ["CANCELLED", "ALLTRADED"])
def test_cta_terminal_order_before_probe_trade_waits_for_trade_callback(
    terminal_status: str,
) -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(["OPEN.1"], role="open", price=3500, volume=1)
    strategy._pending_entry = PendingEntry(intent, probe_intent_id=intent_id)
    strategy._risk = risk
    strategy._position_context = None
    strategy._exit_manager = SimpleNamespace(on_position_opened=lambda context: None)
    strategy.bars_processed = 1
    strategy.order_status = "pending_open"
    strategy.put_event = lambda: None
    strategy.write_log = lambda message: None
    strategy._sync_runtime_state_checkpoint = lambda: None

    strategy.on_order(
        SimpleNamespace(
            vt_orderid="OPEN.1",
            price=3500,
            volume=1,
            traded=1,
            status=SimpleNamespace(name=terminal_status),
            is_active=lambda: False,
        )
    )

    assert strategy._pending_entry is not None
    assert "OPEN.1" in strategy._order_state.active
    assert strategy._order_state.active["OPEN.1"].status == (
        "TERMINAL_PENDING_TRADES"
    )
    assert risk.get_state()["option_d"]["reserved_intent_id"] == intent_id

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.OPEN,
            direction=SimpleNamespace(name="LONG"),
            vt_tradeid="OPEN.FILL.1",
            vt_orderid="OPEN.1",
            volume=1,
            price=3500,
            symbol="RB2610",
            datetime=datetime(2026, 8, 14, 9, 16),
        )
    )

    assert strategy._pending_entry is None
    assert "OPEN.1" not in strategy._order_state.active
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_IN_FLIGHT"


def test_cta_rejected_probe_then_late_open_fill_requires_recovery() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.REJECTED"])
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(
        ["OPEN.REJECTED"], role="open", price=3500, volume=1
    )
    strategy._pending_entry = PendingEntry(intent, probe_intent_id=intent_id)
    strategy._risk = risk
    strategy._position_context = None
    strategy.order_status = "pending_open"
    strategy.put_event = lambda: None
    strategy.write_log = lambda message: None
    checkpoints: list[str] = []
    strategy._sync_runtime_state_checkpoint = lambda: checkpoints.append("saved")

    strategy.on_order(
        SimpleNamespace(
            vt_orderid="OPEN.REJECTED",
            price=3500,
            volume=1,
            traded=0,
            status=SimpleNamespace(name="REJECTED"),
            is_active=lambda: False,
        )
    )

    assert strategy._pending_entry is None
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_AVAILABLE"
    assert risk.get_state()["option_d"]["reserved_intent_id"] is None
    assert checkpoints == ["saved"]

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.OPEN,
            direction=Direction.LONG,
            vt_tradeid="OPEN.LATE.FILL",
            vt_orderid="OPEN.REJECTED",
            volume=1,
            price=3500,
        )
    )

    assert strategy._position_context is None
    assert strategy._recovery_required
    assert (
        "unreconciled_open_fill:late:OPEN.REJECTED:OPEN.LATE.FILL"
        in strategy.recovery_reasons
    )
    assert checkpoints == ["saved", "saved"]
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_AVAILABLE"


def test_pre_release_live_engine_alltraded_before_probe_trade_reconciles() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.LIVE"])
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._pending_order_ids = {"OPEN.LIVE"}
    engine._pending_entries = {
        "OPEN.LIVE": PendingEntry(intent, probe_intent_id=intent_id)
    }
    engine._terminal_reported_traded = {}
    engine._risk = risk
    engine._position = None
    engine._bar_count = 1
    engine._exit_manager = SimpleNamespace(
        on_position_opened=lambda context: None,
        on_position_updated=lambda context: None,
    )
    engine._log = lambda message: None

    engine._on_order(
        SimpleNamespace(
            data=SimpleNamespace(
                vt_orderid="OPEN.LIVE",
                price=3500,
                volume=1,
                traded=1,
                status=Status.ALLTRADED,
                is_active=lambda: False,
            )
        )
    )

    assert "OPEN.LIVE" in engine._pending_order_ids
    assert engine._terminal_reported_traded == {"OPEN.LIVE": 1}
    assert risk.get_state()["option_d"]["reserved_intent_id"] == intent_id

    engine._on_trade(
        SimpleNamespace(
            data=SimpleNamespace(
                offset=Offset.OPEN,
                direction=Direction.LONG,
                vt_tradeid="OPEN.LIVE.FILL",
                vt_orderid="OPEN.LIVE",
                volume=1,
                price=3500,
                symbol="RB2610",
                datetime=datetime(2026, 8, 14, 9, 16),
            )
        )
    )

    assert "OPEN.LIVE" not in engine._pending_order_ids
    assert engine._terminal_reported_traded == {}
    assert risk.get_state()["option_d"]["regime_state"] == "PROBE_IN_FLIGHT"


def test_cta_runtime_package_restores_reserved_probe_intent_and_order() -> None:
    risk = _risk()
    _pause(risk)
    risk.advance_session("2026-08-14")
    intent_id, intent = _reserve(risk)
    risk.bind_probe_orders(intent_id, ["OPEN.1"])

    source = ChanBspStrategy.__new__(ChanBspStrategy)
    source._order_state = CtaOrderStatusMachine()
    source._order_state.submit(["OPEN.1"], role="open", price=3500, volume=1)
    source._pending_entry = PendingEntry(intent, probe_intent_id=intent_id)
    source._risk = risk
    source._position_context = None
    source._persist_runtime_state()

    target = ChanBspStrategy.__new__(ChanBspStrategy)
    target.runtime_state_json = source.runtime_state_json
    target.order_state_json = ""
    target.risk_state_json = ""
    target.position_state_json = ""
    target._order_state = CtaOrderStatusMachine()
    target._risk = _risk()
    target._exit_manager = SimpleNamespace(on_position_opened=lambda context: None)
    target._runtime_state_restored = False
    target.order_status = "idle"
    target.risk_status = "unknown"
    target.write_log = lambda message: None

    target._restore_runtime_state()

    assert target._pending_entry.probe_intent_id == intent_id
    assert target._pending_entry.intent == intent
    assert "OPEN.1" in target._order_state.active
    assert target._risk.get_state()["option_d"]["reserved_intent_id"] == intent_id
