"""P7.5 failure matrix: end-to-end fault scenarios against the hardened CTA.

Each scenario drives the real ChanBspStrategy through a fault and asserts the
safe outcome (block opens, allow closes, never auto-claim, never double-fill).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from signal_core.models import SignalDecision, SignalDirection
from strategy_policy.position import PositionContext
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.hard_risk import HardRiskState, evaluate_open_guard
from vnpy_chan.oms_reconciliation import reconcile
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.runtime_state import RuntimeStatePackage


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────

def _position(direction: str, volume: int):
    return SimpleNamespace(symbol="rb2610", direction=direction, volume=volume)


def _order(vt_orderid: str, status: str, volume: float = 1, traded: float = 0):
    return SimpleNamespace(
        vt_orderid=vt_orderid,
        orderid=vt_orderid,
        price=3500,
        volume=volume,
        traded=traded,
        status=SimpleNamespace(name=status),
        is_active=lambda: status not in {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED"},
    )


def _trade(vt_orderid: str, tradeid: str, volume: float, offset=None, price: float = 3500):
    return SimpleNamespace(
        vt_orderid=vt_orderid,
        tradeid=tradeid,
        volume=volume,
        offset=offset,
        price=price,
        symbol="rb2610",
    )


def _context() -> PositionContext:
    return PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500,
        entry_time="2026-08-04 09:01",
        entry_bar=10,
        volume=1,
    )


def _strategy(**overrides) -> ChanBspStrategy:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.shadow_mode = True
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.write_log = lambda message: None
    strategy.put_event = lambda: None
    strategy._persist_runtime_state = lambda: None
    strategy._order_state = CtaOrderStatusMachine()
    strategy._risk = None
    strategy._position_context = None
    strategy._recovery_required = False
    strategy._recovery_result = None
    strategy._last_tick_time = datetime.now()
    strategy._last_account_time = datetime.now()
    strategy.hard_risk_status = "armed"
    strategy.hard_risk_reasons = ""
    strategy.manual_halt = False
    strategy.minimum_equity = 0.0
    strategy.active_main_contracts = "rb2610"
    strategy.total_pnl = 0.0
    strategy.total_trades = 0
    strategy._trade_pnl_batch = []
    strategy._pending_entry = None
    strategy._pending_reversal = None
    strategy._exit_manager = None
    strategy._config = SimpleNamespace(
        execution=SimpleNamespace(fee_points=1.0, slippage_points=1.0, price_tick=1.0),
    )
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="shadow_mode_enabled",
    )
    for name, value in overrides.items():
        setattr(strategy, name, value)
    return strategy


# ─────────────────────────────────────────────────────────────
# 1. Partial fill then full fill
# ─────────────────────────────────────────────────────────────

def test_partial_fill_then_full_fill() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=2)
    machine.on_order(_order("SIM.1", "NOTTRADED", volume=2))
    machine.on_trade(_trade("SIM.1", "T1", 1, offset="OPEN"))
    assert machine.active["SIM.1"].status == "PARTTRADED"
    machine.on_trade(_trade("SIM.1", "T2", 1, offset="OPEN"))
    assert machine.summary == "idle"
    assert machine.history[-1].status == "ALLTRADED"


# ─────────────────────────────────────────────────────────────
# 2. Rejection clears pending entry
# ─────────────────────────────────────────────────────────────

def test_rejected_open_clears_pending_entry() -> None:
    strategy = _strategy()
    strategy._order_state.submit(["SIM.1"], role="open", price=3500, volume=1)
    strategy._pending_entry = object()
    strategy.on_order(_order("SIM.1", "REJECTED", volume=1, traded=0))
    assert strategy._pending_entry is None
    assert strategy.order_status == "idle"


# ─────────────────────────────────────────────────────────────
# 3. Duplicate fill dropped
# ─────────────────────────────────────────────────────────────

def test_duplicate_fill_dropped() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_trade(_trade("SIM.1", "T1", 1))
    machine.on_trade(_trade("SIM.1", "T1", 1))  # duplicate
    assert len(machine.consumed_trade_ids) == 1
    assert machine.history[-1].traded == 1


# ─────────────────────────────────────────────────────────────
# 4. Out-of-order report (terminal then trade) ignored
# ─────────────────────────────────────────────────────────────

def test_out_of_order_trade_after_cancel_ignored() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_order(_order("SIM.1", "CANCELLED"))
    machine.on_trade(_trade("SIM.1", "T9", 1))
    assert len(machine.consumed_trade_ids) == 0
    assert machine.history[-1].traded == 0


# ─────────────────────────────────────────────────────────────
# 5. Risk intercept (hard risk) blocks open, allows close
# ─────────────────────────────────────────────────────────────

def test_risk_intercept_blocks_open_allows_close() -> None:
    decision = evaluate_open_guard(
        HardRiskState(
            vt_symbol="rb2610.SHFE",
            planned_open_lots=2,
            manual_halt=False,
            account_equity=100000,
            last_tick_time=datetime.now(),
            last_account_time=datetime.now(),
        )
    )
    assert not decision.allowed
    assert any("single_lot_limit" in r for r in decision.reasons)

    # close path bypasses the guard entirely (shadow mode returns [] but
    # the sender is invoked, proving a close is never hard-risk-blocked)
    strategy = _strategy(shadow_mode=False)
    called = []
    result = strategy._send_live_or_shadow(
        "close",
        lambda: (called.append(1), ["SIM.1"])[1],
        price=3500,
        volume=1,
        closing=True,
        reason="stop",
    )
    assert result == ["SIM.1"]
    assert called == [1]


# ─────────────────────────────────────────────────────────────
# 6. Disconnect / reconnect: stale tick blocks open
# ─────────────────────────────────────────────────────────────

def test_disconnect_stale_tick_blocks_open() -> None:
    from datetime import timedelta

    decision = evaluate_open_guard(
        HardRiskState(
            vt_symbol="rb2610.SHFE",
            planned_open_lots=1,
            account_equity=100000,
            last_tick_time=datetime.now() - timedelta(minutes=10),
            last_account_time=datetime.now(),
        )
    )
    assert not decision.allowed
    assert any("market_heartbeat_timeout" in r for r in decision.reasons)


# ─────────────────────────────────────────────────────────────
# 7. Restart with position: state restore + OMS aligned
# ─────────────────────────────────────────────────────────────

def test_restart_with_position_restores_and_aligns() -> None:
    source = _strategy()
    source._position_context = _context()
    source._order_state_json = ""
    source.risk_state_json = ""
    source._risk = None
    source._decision_kernel = None
    source._persist_runtime_state = ChanBspStrategy._persist_runtime_state.__get__(source)
    source._ensure_runtime_state_helpers = (
        ChanBspStrategy._ensure_runtime_state_helpers.__get__(source)
    )
    source._build_runtime_state_package = (
        ChanBspStrategy._build_runtime_state_package.__get__(source)
    )
    source._runtime_strategy_id = ChanBspStrategy._runtime_strategy_id.__get__(source)
    source._exit_state_snapshot = lambda: {}
    source._trade_ids = lambda: []
    source._persist_runtime_state()
    package = RuntimeStatePackage.from_json(
        source.runtime_state_json,
        expected_strategy_id="chan.py|rb2610.SHFE",
    )
    assert package.position is not None

    result = reconcile(
        oms_positions=[_position("LONG", 1)],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=_context(),
        strategy_active_order_ids=(),
    )
    assert result.aligned


# ─────────────────────────────────────────────────────────────
# 8. Unknown position: never auto-claimed, RECOVERY_REQUIRED
# ─────────────────────────────────────────────────────────────

def test_unknown_position_forces_recovery() -> None:
    result = reconcile(
        oms_positions=[_position("LONG", 1)],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert not result.allow_open
    assert result.allow_close
    assert any("unattributed_position" in r for r in result.reasons)


# ─────────────────────────────────────────────────────────────
# 9. Active order recovery: OMS active order not in strategy
# ─────────────────────────────────────────────────────────────

def test_active_order_recovery_mismatch() -> None:
    result = reconcile(
        oms_positions=[],
        oms_orders=[_order("SIM.9", "SUBMITTING")],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert any("active_order_mismatch" in r for r in result.reasons)


# ─────────────────────────────────────────────────────────────
# 10. Contract rollover: bound contract no longer main
# ─────────────────────────────────────────────────────────────

def test_contract_rollover_blocks_open() -> None:
    decision = evaluate_open_guard(
        HardRiskState(
            vt_symbol="rb2610.SHFE",
            planned_open_lots=1,
            account_equity=100000,
            last_tick_time=datetime.now(),
            last_account_time=datetime.now(),
            active_main_contracts=("rb2601",),
        )
    )
    assert not decision.allowed
    assert any("contract_rollover_required" in r for r in decision.reasons)


def test_contract_rollover_aligned_when_in_main_list() -> None:
    decision = evaluate_open_guard(
        HardRiskState(
            vt_symbol="rb2610.SHFE",
            planned_open_lots=1,
            account_equity=100000,
            last_tick_time=datetime.now(),
            last_account_time=datetime.now(),
            active_main_contracts=("rb2610",),
        )
    )
    assert decision.allowed
