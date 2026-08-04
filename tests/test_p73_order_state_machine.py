from __future__ import annotations

from types import SimpleNamespace

from vnpy_chan.order_state import (
    CtaOrderStatusMachine,
    RISK_REJECTED_ROLE,
)


def _order(vt_orderid: str, status: str, traded: float = 0, volume: float = 2):
    return SimpleNamespace(
        vt_orderid=vt_orderid,
        price=3500,
        volume=volume,
        traded=traded,
        status=SimpleNamespace(name=status),
        is_active=lambda: status not in {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED"},
    )


def _trade(vt_orderid: str, tradeid: str, volume: float, price: float = 3500):
    return SimpleNamespace(
        vt_orderid=vt_orderid,
        tradeid=tradeid,
        volume=volume,
        price=price,
    )


def test_lifecycle_submit_to_full_fill() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=2)
    assert machine.summary == "SUBMITTING:1"

    machine.on_order(_order("SIM.1", "NOTTRADED"))
    machine.on_trade(_trade("SIM.1", "T1", 1))
    assert machine.active["SIM.1"].status == "PARTTRADED"

    machine.on_trade(_trade("SIM.1", "T2", 1))
    assert machine.summary == "idle"
    assert machine.history[-1].status == "ALLTRADED"


def test_duplicate_fill_is_dropped() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)

    machine.on_trade(_trade("SIM.1", "T1", 1))
    machine.on_trade(_trade("SIM.1", "T1", 1))  # duplicate tradeid

    assert machine.history[-1].traded == 1
    assert len(machine.consumed_trade_ids) == 1


def test_terminal_order_rejects_late_trade() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_order(_order("SIM.1", "CANCELLED"))
    machine.on_order(_order("SIM.1", "CANCELLED"))
    assert machine.summary == "idle"

    # out-of-order report after archive must not resurrect traded volume
    machine.on_trade(_trade("SIM.1", "T9", 1))
    assert len(machine.consumed_trade_ids) == 0
    assert all(not order.active for order in machine.history)


def test_cancelling_keeps_order_active() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)

    machine.on_order(_order("SIM.1", "CANCELLING"))
    assert machine.summary == "CANCELLING:1"

    machine.on_order(_order("SIM.1", "CANCELLED"))
    assert machine.summary == "idle"


def test_empty_order_id_records_risk_rejected() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit([""], role="open", price=3500, volume=1, reason="entry")

    assert machine.summary == "idle"
    assert machine.history[-1].role == RISK_REJECTED_ROLE
    assert "empty_vt_orderid" in machine.history[-1].reason


def test_timeout_archives_order() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(
        ["SIM.1"],
        role="open",
        price=3500,
        volume=1,
        timeout_at="2026-08-04T10:30:00",
    )
    timed_out = machine.check_timeouts("2026-08-04T10:31:00")

    assert timed_out == ["SIM.1"]
    assert machine.summary == "idle"
    assert machine.history[-1].status == "TIMEDOUT"


def test_consumed_trade_ids_survive_json_roundtrip() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_trade(_trade("SIM.1", "T1", 1))

    restored = CtaOrderStatusMachine.from_json(machine.to_json())
    assert restored.consumed_trade_ids == {"T1"}
    # the duplicate fill after restore is also dropped
    machine.on_trade(_trade("SIM.1", "T1", 1))
    assert machine.history[-1].traded == 1


def test_strategy_refuses_second_open_intent_while_pending() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
    from signal_core.models import SignalDecision
    from chan_futures.trade_intent import TradeIntent
    from chan_futures.strategy import StrategySignal

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy.write_log = lambda message: None
    sentinel = object()
    strategy._pending_entry = sentinel  # one open intent already in flight

    signal = StrategySignal(
        timestamp="2026-08-04 09:15",
        action="open_long",
        target_position=1,
        price=3500,
        reason="entry",
        bsp_type="BSP1",
        bsp_bi_idx=0,
        bsp_klu_idx=0,
    )
    intent = TradeIntent(
        signal=signal,
        decision=SignalDecision(
            decision_id="D1",
            event_id="E1",
            policy_id="p",
            accepted=True,
            reason_codes=(),
            entry_price_hint=3500,
            invalidation_price=None,
            initial_stop_price=None,
            position_size_hint=1,
            decided_at="2026-08-04 09:15",
        ),
    )
    strategy._submit_open_intent(intent)

    # refusal leaves no new pending entry
    assert strategy._pending_entry is sentinel  # unchanged


def test_reversal_waits_for_zero_position() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
    from chan_futures.trade_intent import PendingEntry, TradeIntent
    from chan_futures.strategy import StrategySignal
    from signal_core.models import SignalDecision
    from strategy_policy.position import PositionContext
    from signal_core.models import SignalDirection

    reversal_intent = TradeIntent(
        signal=StrategySignal(
            timestamp="2026-08-04 10:00",
            action="open_short",
            target_position=-1,
            price=3490,
            reason="reverse",
            bsp_type="BSP1",
            bsp_bi_idx=0,
            bsp_klu_idx=0,
        ),
        decision=SignalDecision(
            decision_id="D2",
            event_id="E2",
            policy_id="p",
            accepted=True,
            reason_codes=(),
            entry_price_hint=3490,
            invalidation_price=None,
            initial_stop_price=None,
            position_size_hint=1,
            decided_at="2026-08-04 10:00",
        ),
    )
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy.write_log = lambda message: None
    strategy._pending_reversal = PendingEntry(intent=reversal_intent)
    strategy._pending_entry = None
    strategy._exit_manager = None
    strategy.total_pnl = 0.0
    strategy.total_trades = 0
    strategy._trade_pnl_batch = []
    strategy._position_context = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500,
        entry_time="2026-08-04 09:01",
        entry_bar=10,
        volume=1,
    )
    strategy.put_event = lambda: None
    strategy._persist_runtime_state = lambda: None
    strategy._risk = None
    strategy._config = SimpleNamespace(
        execution=SimpleNamespace(fee_points=1.0, slippage_points=1.0, price_tick=1.0)
    )
    strategy.shadow_mode = True
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.recovery_reasons = ""
    strategy._recovery_required = False
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="shadow_mode_enabled",
    )
    strategy._order_state.consumed_trade_ids = set()

    # simulate close fill that brings position to zero
    from vnpy.trader.constant import Offset

    strategy.on_trade(
        SimpleNamespace(
            vt_orderid="SIM.1",
            offset=Offset.CLOSE,
            price=3500,
            volume=1,
            symbol="rb2610",
            datetime=None,
        )
    )

    # position reached zero; reversal intent consumed
    assert strategy._pending_reversal is None
    assert strategy.position_context is None

