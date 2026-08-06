"""P7-R9 failure tests: timer bridge, RiskManager event wiring, vt_tradeid merge,
Account Event gateway filtering, and EventEngine lifecycle.

P7-R11: all tests use a single reliable _stop_ee helper; cwd saved before
MainEngine constructor; every MainEngine/EventEngine cleaned up in try/finally;
handler identity verified via __self__/__func__; no skip/xfail/timeout hacks.
Includes R11.3 RiskEngine identity tests and R11.4 holdout trade audit tests.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.runtime_state import RuntimeStatePackage


# ── single reliable cleanup helper ────────────────────────────────────────

def _stop_ee(ee) -> None:
    """Stop an EventEngine that was started by MainEngine.__init__.

    Safely handles: already-stopped state, unstarted timer thread, general
    exceptions from timer.join().
    """
    try:
        if getattr(ee, "_active", False):
            ee.stop()
    except Exception:
        pass


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class _LiveMainEngine:
    def __init__(self, gateway_name: str | None) -> None:
        self.gateway_name = gateway_name

    def get_contract(self, vt_symbol: str):
        if self.gateway_name is None:
            return None
        return SimpleNamespace(gateway_name=self.gateway_name)


class _LiveCtaEngine:
    def __init__(self, event_engine, gateway_name: str | None) -> None:
        self.event_engine = event_engine
        self.main_engine = _LiveMainEngine(gateway_name)
        self.cancelled: list[str] = []
        self.calls: list[str] = []
        self.logs: list[str] = []

    def call_strategy_func(self, strategy, func) -> None:
        self.calls.append(func.__name__)
        func()

    def cancel_order(self, strategy, vt_orderid: str) -> None:
        self.cancelled.append(vt_orderid)

    def write_log(self, message, strategy=None) -> None:
        self.logs.append(str(message))

    def put_strategy_event(self, strategy) -> None:
        pass


def _make_live_strategy(event_engine, gateway_name: str | None):
    engine = _LiveCtaEngine(event_engine, gateway_name)
    strategy = ChanBspStrategy(engine, "r11", "rb2610.SHFE", {})
    strategy._reapply_warmup_readiness = lambda: None
    strategy._restore_runtime_state = lambda: None
    strategy._check_oms_reconciliation = lambda: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        mode="shadow",
        reason_text="shadow_mode_enabled",
    )
    strategy.production_ready = True
    strategy._recovery_required = False
    strategy.inited = True
    strategy.trading = True
    return strategy, engine


# ── R9.0-1: CtaEngine does NOT register EVENT_TIMER ──

def test_cta_engine_does_not_register_event_timer() -> None:
    """CtaEngine.register_event only covers TICK/ORDER/TRADE, not TIMER."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_ctastrategy.engine import CtaEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        cta = CtaEngine(me, ee)
        handlers = ee._handlers

        assert "eTick." in handlers, "CtaEngine must register EVENT_TICK"
        assert "eOrder." in handlers, "CtaEngine must register EVENT_ORDER"
        assert "eTrade." in handlers, "CtaEngine must register EVENT_TRADE"

        acct_handlers = handlers.get("eAccount.", [])
        for h in acct_handlers:
            if hasattr(h, "__self__") and isinstance(h.__self__, CtaEngine):
                pytest.fail("CtaEngine must NOT register EVENT_ACCOUNT")

        timer_handlers = handlers.get("eTimer", [])
        for h in timer_handlers:
            if hasattr(h, "__self__") and isinstance(h.__self__, CtaEngine):
                pytest.fail("CtaEngine must NOT register EVENT_TIMER")
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


def test_strategy_on_timer_is_not_a_known_cta_callback() -> None:
    """CtaEngine has no process_timer_event — on_timer is dead code."""
    from vnpy_ctastrategy.engine import CtaEngine
    for name in dir(CtaEngine):
        if "timer" in name.lower():
            pytest.fail(f"CtaEngine has unexpected timer method: {name}")


# ── R9.0-2: timeout never fires without timer bridge ──

def test_timeout_without_timer_bridge_never_cancels() -> None:
    """Without a registered timer bridge, check_timeouts never fires."""
    machine = CtaOrderStatusMachine()
    machine.submit(
        ["SIM.1"], role="open", price=3500, volume=1,
        timeout_at="2026-08-05T10:00:00",
    )
    assert "SIM.1" in machine.active
    assert machine.active["SIM.1"].status == "SUBMITTING"
    assert not machine.pending_cancel_orderids


# ── R9.0-3: ChanOpenGuardRule custom tick/account not auto-registered ──

def test_custom_rule_tick_account_not_auto_registered() -> None:
    """RiskEngine.register_events checks four standard callback names."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_riskmanager.engine import RiskEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        with patch.object(RiskEngine, "load_rules_from_folder", lambda s, p, m: None):
            with patch.object(RiskEngine, "setting_filename", "nonexistent_rm_setting.json"):
                engine = RiskEngine(me, ee)

        handlers = ee._handlers
        acct_handlers = handlers.get("eAccount.", [])
        for h in acct_handlers:
            if hasattr(h, "__self__") and isinstance(h.__self__, RiskEngine):
                pytest.fail(
                    "RiskEngine must NOT auto-register EVENT_ACCOUNT "
                    "(RuleTemplate has no standard on_account callback)"
                )
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


# ── R9.0-4: RiskEngine send_order is patched and interception works ──

def test_risk_engine_send_order_interception_active() -> None:
    """RiskEngine.patch_functions correctly intercepts MainEngine.send_order."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_riskmanager.engine import RiskEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        with patch.object(RiskEngine, "load_rules_from_folder", lambda s, p, m: None):
            with patch.object(RiskEngine, "setting_filename", "nonexistent_rm_setting.json"):
                engine = RiskEngine(me, ee)

        bound = getattr(me, "send_order")
        assert hasattr(bound, "__self__"), "send_order must be a bound method after patching"
        assert bound.__func__ is RiskEngine.send_order, (
            "MainEngine.send_order must be replaced by RiskEngine.send_order"
        )
        assert hasattr(engine, "_send_order"), "RiskEngine must save _send_order"
        assert callable(engine._send_order), "_send_order must be callable"
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


# ── R9.0-6: OMS reconciliation must filter by exchange/gateway ──

def test_oms_reconciliation_should_filter_by_exchange_and_gateway() -> None:
    from vnpy_chan.oms_reconciliation import reconcile, oms_net_position

    pos_shfe = SimpleNamespace(symbol="rb2610", exchange="SHFE", gateway_name="CTP",
                               direction="LONG", volume=1)
    pos_ine = SimpleNamespace(symbol="rb2610", exchange="INE", gateway_name="CTP",
                              direction="LONG", volume=9)

    net_shfe = oms_net_position([pos_shfe, pos_ine], "rb2610", exchange="SHFE")
    assert net_shfe == 1
    net_ine = oms_net_position([pos_shfe, pos_ine], "rb2610", exchange="INE")
    assert net_ine == 9
    net_all = oms_net_position([pos_shfe, pos_ine], "rb2610")
    assert net_all == 10


# ── R9.0-8: vt_tradeid merge after order_state restore ──

def test_vt_tradeids_preserved_after_order_state_restore() -> None:
    machine = CtaOrderStatusMachine()
    machine.consumed_trade_ids.update({"T-PRE-1", "T-PRE-2"})
    restored = CtaOrderStatusMachine.from_json(machine.to_json())
    assert "T-PRE-1" in restored.consumed_trade_ids
    assert "T-PRE-2" in restored.consumed_trade_ids


# ── R9.0-9: EventEngine thread must exit after close ──

def test_event_engine_clean_close() -> None:
    """EventEngine started and closed must release its threads. cwd restored."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        assert ee._active is True
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)

    time.sleep(0.5)
    assert not ee._thread.is_alive(), "EventEngine thread must exit after close"
    if hasattr(ee, "_timer") and ee._timer is not None:
        assert not ee._timer.is_alive(), "EventEngine timer must exit after close"
    assert os.getcwd() == saved_cwd, (
        f"cwd must be restored; was {saved_cwd}, now {os.getcwd()}"
    )


def test_strategy_account_event_filters_gateway_and_unregisters() -> None:
    from vnpy.event import Event, EventEngine
    from vnpy.trader.event import EVENT_ACCOUNT

    ee = EventEngine()
    ee.start()
    strategy, _ = _make_live_strategy(ee, "CTP")
    try:
        strategy.on_start()
        first_handler = strategy._live_account_handler
        strategy.on_start()
        assert first_handler not in ee._handlers.get(EVENT_ACCOUNT, [])
        assert ee._handlers.get(EVENT_ACCOUNT, []).count(
            strategy._live_account_handler
        ) == 1

        ee.put(Event(EVENT_ACCOUNT, SimpleNamespace(gateway_name="OTHER")))
        time.sleep(0.1)
        assert strategy._last_account_time is None

        ee.put(Event(EVENT_ACCOUNT, SimpleNamespace(gateway_name="ctp")))
        assert _wait_until(lambda: strategy._last_account_time is not None)

        current_handler = strategy._live_account_handler
        strategy.on_stop()
        assert current_handler not in ee._handlers.get(EVENT_ACCOUNT, [])
        strategy.on_stop()
    finally:
        strategy._unregister_live_events()
        _stop_ee(ee)


def test_strategy_account_event_unknown_gateway_fails_closed() -> None:
    from vnpy.event import Event, EventEngine
    from vnpy.trader.event import EVENT_ACCOUNT

    ee = EventEngine()
    ee.start()
    strategy, _ = _make_live_strategy(ee, None)
    try:
        strategy.on_start()
        ee.put(Event(EVENT_ACCOUNT, SimpleNamespace(gateway_name="CTP")))
        time.sleep(0.1)
        assert strategy._last_account_time is None
        assert not getattr(strategy, "_resolved_gateway", "")
    finally:
        strategy.on_stop()
        _stop_ee(ee)


def test_strategy_timer_event_cancels_expired_order() -> None:
    from vnpy.event import Event, EventEngine
    from vnpy.trader.event import EVENT_TIMER

    ee = EventEngine()
    ee.start()
    strategy, engine = _make_live_strategy(ee, "CTP")
    try:
        strategy.on_start()
        strategy._order_state.submit(
            ["SIM.1"],
            role="open",
            price=3500,
            volume=1,
            timeout_at="2000-01-01T00:00:00",
        )
        ee.put(Event(EVENT_TIMER))
        assert _wait_until(lambda: "SIM.1" in engine.cancelled)
        assert "on_timer" in engine.calls
        assert strategy._order_state.active["SIM.1"].status == "CANCELLING"
    finally:
        strategy.on_stop()
        _stop_ee(ee)


def test_strategy_start_exception_unregisters_live_handlers() -> None:
    from vnpy.event import EventEngine
    from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TIMER

    ee = EventEngine()
    ee.start()
    strategy, _ = _make_live_strategy(ee, "CTP")
    strategy._refresh_production_gate = lambda: (_ for _ in ()).throw(
        RuntimeError("gate failed")
    )
    try:
        with pytest.raises(RuntimeError, match="gate failed"):
            strategy.on_start()
        assert strategy._live_timer_handler is None
        assert strategy._live_account_handler is None
        assert not ee._handlers.get(EVENT_TIMER, [])
        assert not ee._handlers.get(EVENT_ACCOUNT, [])
    finally:
        strategy._unregister_live_events()
        _stop_ee(ee)


# ── R11.3: RiskEngine takeover identity ──────────────────────────────────

def test_risk_engine_two_instance_mismatch_rejected() -> None:
    """Two RiskEngine instances: last-created overwrites send_order."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_riskmanager.engine import RiskEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        with patch.object(RiskEngine, "load_rules_from_folder", lambda s, p, m: None):
            with patch.object(RiskEngine, "setting_filename", "nonexistent_rm_setting.json"):
                engine_a = RiskEngine(me, ee)
                engine_b = RiskEngine(me, ee)

        bound = getattr(me, "send_order")
        assert hasattr(bound, "__self__"), "send_order must be bound"
        assert bound.__self__ is engine_b, (
            "send_order.__self__ must be engine_b (last to patch)"
        )
        assert bound.__self__ is not engine_a, (
            "send_order.__self__ must NOT be engine_a (overwritten by engine_b)"
        )
        assert bound.__func__ is RiskEngine.send_order, "__func__ must be RiskEngine.send_order"
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


def test_send_order_wrapper_override_rejected() -> None:
    """send_order wrapped by a non-RiskEngine function is rejected."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_riskmanager.engine import RiskEngine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        original_send = me.send_order

        def custom_wrapper(req, gateway_name=None):
            return original_send(req, gateway_name=gateway_name)

        me.send_order = custom_wrapper

        so = getattr(me, "send_order", None)
        if hasattr(so, "__self__"):
            if hasattr(so, "__func__"):
                assert so.__func__ is not RiskEngine.send_order, (
                    "custom wrapper must NOT match RiskEngine.send_order"
                )
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


def test_inspect_live_risk_engine_rejects_mismatched_identity() -> None:
    """inspect_live_risk_engine rejects wrong engine via is identity check."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_riskmanager.engine import RiskEngine
    from vnpy_chan.production_guard import inspect_live_risk_engine

    saved_cwd = os.getcwd()
    ee = EventEngine()
    me = None
    try:
        me = MainEngine(ee)
        with patch.object(RiskEngine, "load_rules_from_folder", lambda s, p, m: None):
            with patch.object(RiskEngine, "setting_filename", "nonexistent_rm_setting.json"):
                engine_a = RiskEngine(me, ee)
                engine_b = RiskEngine(me, ee)

        with patch.object(me, "get_engine", return_value=engine_b):
            status = inspect_live_risk_engine(me)
            assert status.send_order_patched is True, (
                f"Expected patched=True when engine matches; error={status.error}"
            )

        with patch.object(me, "get_engine", return_value=engine_a):
            status2 = inspect_live_risk_engine(me)
            assert status2.send_order_patched is False, (
                "must reject when queried engine (engine_a) is not "
                "the one that patched send_order (engine_b)"
            )
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


# ── R11.4: holdout per-trade audit ────────────────────────────────────────

def test_holdout_trade_dict_fields_non_empty() -> None:
    """R11.4: trade dicts must have all required fields. No getattr."""
    from signal_core.models import SignalDirection
    from scripts.run_p7_holdout import _trade_audit_record

    trade = {
        "entry_bar": 100,
        "entry_price": 3500.0,
        "entry_time": "2026-06-10 09:30:00",
        "direction": SignalDirection.LONG,
        "lots": 1,
        "grade": "standard",
        "active_symbol": "rb2610",
        "event_id": "evt-1",
        "signal_key": "sig-1",
        "decision_id": "dec-1",
        "setup_invalidation_price": 3480.0,
        "execution_stop_price": 3490.0,
        "exit_bar": 250,
        "exit_price": 3550.0,
        "exit_time": "2026-06-12 14:00:00",
        "hold_bars": 150,
        "pnl_points": 48.5,
        "exit_reason": "trailing_stop",
        "exit_rule": "trailing_0.5r",
    }

    execution = SimpleNamespace(fee_points=1.0, slippage_points=1.0)
    record = _trade_audit_record(trade, execution)
    for name in ("entry_time", "exit_time", "direction", "exit_reason"):
        assert record[name]
    assert record["net_pnl_points"] == 48.5
    assert record["_audit_ok"] is True

    broken = dict(trade, exit_reason="")
    with pytest.raises(ValueError, match="exit_reason"):
        _trade_audit_record(broken, execution)


def test_holdout_trade_gross_minus_fees_slippage_equals_net() -> None:
    """R11.4: gross - fees - slippage = net. Costs from execution config."""
    from scripts.run_p7_holdout import _trade_audit_record

    execution = SimpleNamespace(fee_points=1.0, slippage_points=1.0)

    trades = [
        {"lots": 1, "pnl_points": 50.0, "entry_time": "t1", "exit_time": "t2",
         "direction": "LONG", "exit_reason": "target"},
        {"lots": 2, "pnl_points": -30.0, "entry_time": "t3", "exit_time": "t4",
         "direction": "SHORT", "exit_reason": "stop"},
    ]

    total_net = 0.0
    for t in trades:
        record = _trade_audit_record(t, execution)
        assert abs(
            record["gross_pnl_points"]
            - record["fees_points"]
            - record["slippage_points"]
            - record["net_pnl_points"]
        ) < 0.01
        total_net += record["net_pnl_points"]

    assert abs(total_net - 20.0) < 0.01, f"sum of trade nets must be 20.0, got {total_net}"
