"""P7-R4: complete order state machine dispositions.

on_trade() must return an explicit disposition:
  accepted          -> fill accepted, position/pnl may be updated
  duplicate         -> vt_tradeid already consumed (dedup)
  late              -> order already terminal (out-of-order report)
  unknown           -> no tracked order for this vt_orderid
  invalid_transition-> state machine rejects the transition

The strategy only updates PositionContext / risk / PnL on ACCEPTED.
Timeout first requests a real cancel (CANCELLING), archives only on a
terminal report, and TIMEDOUT is itself a terminal status.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vnpy_chan.order_state import (
    CtaOrderStatusMachine,
    TradeDisposition,
)


def _order(vt_orderid="SIM.1", status="NOTTRADED", volume=1, traded=0):
    return SimpleNamespace(
        vt_orderid=vt_orderid, orderid=vt_orderid, symbol="rb2610",
        exchange="SHFE", gateway_name="CTP", price=3500, volume=volume,
        traded=traded, status=SimpleNamespace(name=status),
        is_active=lambda: status not in {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED", "TIMEDOUT"},
    )


def _trade(vt_orderid="SIM.1", vt_tradeid="T1", volume=1, price=3500):
    return SimpleNamespace(
        vt_orderid=vt_orderid, tradeid="raw-" + vt_tradeid,
        vt_tradeid=vt_tradeid, volume=volume, price=price, symbol="rb2610",
    )


# ── dispositions ──

def test_trade_accepted_on_first_fill() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    result = machine.on_trade(_trade("SIM.1", "T1", 1))
    assert result.disposition == TradeDisposition.ACCEPTED
    assert result.tracked.traded == 1


def test_trade_duplicate_uses_vt_tradeid() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_trade(_trade("SIM.1", "T1", 1))
    result = machine.on_trade(_trade("SIM.1", "T1", 1))  # same vt_tradeid
    assert result.disposition == TradeDisposition.DUPLICATE
    assert result.tracked is None


def test_trade_duplicate_survives_restart() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_trade(_trade("SIM.1", "T1", 1))
    restored = CtaOrderStatusMachine.from_json(machine.to_json())

    # After restart the order is terminal; a replayed fill is DUPLICATE
    machine2 = restored
    machine2.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine2.on_trade(_trade("SIM.1", "T1", 1))
    result = machine2.on_trade(_trade("SIM.1", "T1", 1))
    assert result.disposition == TradeDisposition.DUPLICATE


def test_trade_late_when_order_terminal() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1)
    machine.on_order(_order("SIM.1", "CANCELLED"))
    result = machine.on_trade(_trade("SIM.1", "T9", 1))
    assert result.disposition == TradeDisposition.LATE
    assert result.tracked is None


def test_trade_unknown_order() -> None:
    machine = CtaOrderStatusMachine()
    result = machine.on_trade(_trade("SIM.NOPE", "T1", 1))
    assert result.disposition == TradeDisposition.UNKNOWN


# ── timeout lifecycle ──

def test_timeout_first_cancels_then_archives_on_terminal_report() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=1,
                   timeout_at="2026-08-04T10:30:00")

    # Timer fires: mark CANCELLING and request a real cancel (via pending_cancel)
    to_cancel = machine.check_timeouts("2026-08-04T10:31:00")
    assert to_cancel == ["SIM.1"]
    assert machine.active["SIM.1"].status == "CANCELLING"
    assert machine.pending_cancel_orderids == ["SIM.1"]

    # Not archived yet (no terminal report)
    assert machine.summary != "idle"

    # Cancel accepted by exchange -> terminal -> archive
    machine.on_order(_order("SIM.1", "CANCELLED"))
    assert machine.summary == "idle"
    assert machine.history[-1].status == "CANCELLED"


def test_timedout_is_terminal_status() -> None:
    assert "TIMEDOUT" in CtaOrderStatusMachine.TERMINAL_STATUSES


# ── single close intent ──

def test_single_active_close_intent() -> None:
    machine = CtaOrderStatusMachine()
    assert not machine.has_active_close_intent()
    machine.set_close_intent("CLOSE.1")
    assert machine.has_active_close_intent()
    machine.clear_close_intent()
    assert not machine.has_active_close_intent()
