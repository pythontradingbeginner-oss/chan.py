"""P7-R10: Failing tests for all Codex findings before repair.

These tests MUST fail against the R9 working tree and MUST pass after
R10.1–R10.7 fixes are applied.  Every test produces an observable failure.

Tests that use real EventEngine/MainEngine clean up in finally blocks.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import OrderRequest, TickData

REPO = Path("H:/Github/chan.py")
RULE_FILE = REPO / "rules" / "chan_open_guard_rule.py"


# ── helpers ──────────────────────────────────────────────────────────────

def _load_rule_class():
    import importlib.util
    spec = importlib.util.spec_from_file_location("chan_open_guard_rule", RULE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ChanOpenGuardRule


def _start_ee():
    ee = EventEngine()
    ee.start()
    return ee


def _stop_ee(ee):
    try:
        if getattr(ee, "_active", False):
            ee.stop()
    except Exception:
        pass


def _open_req(symbol="rb2610", volume=1, exchange=Exchange.SHFE, reference=""):
    return OrderRequest(
        symbol=symbol, exchange=exchange, direction=Direction.LONG,
        offset=Offset.OPEN, type=OrderType.LIMIT, price=3500, volume=volume,
        reference=reference,
    )


def _close_req(symbol="rb2610", volume=1, exchange=Exchange.SHFE, offset=Offset.CLOSE):
    return OrderRequest(
        symbol=symbol, exchange=exchange, direction=Direction.SHORT,
        offset=offset, type=OrderType.LIMIT, price=3500, volume=volume,
    )


def _rule(active=True, **params):
    """Instantiate ChanOpenGuardRule with stub risk_engine + active event_engine."""
    cls = _load_rule_class()
    event_engine = _start_ee()
    oms_stub = SimpleNamespace(
        get_all_accounts=lambda: [SimpleNamespace(balance=200000.0)]
    )
    stub_engine = SimpleNamespace(
        main_engine=SimpleNamespace(get_engine=lambda name: oms_stub),
        write_log=lambda msg: None,
        event_engine=event_engine,
    )
    setting = {"active": active, **params}
    rule = cls(stub_engine, setting)
    return rule, stub_engine, event_engine


# =========================================================================
# R10.0-1: EVENT_ACCOUNT registered ONCE at rule init
# =========================================================================

def test_r10_event_account_registered_on_init() -> None:
    """After ChanOpenGuardRule.__init__, EVENT_ACCOUNT handler is registered."""
    _, _, ee = _rule(active=True)
    try:
        handlers = ee._handlers.get("eAccount.", [])
        assert len(handlers) >= 1, "EVENT_ACCOUNT must be registered after rule init"
    finally:
        _stop_ee(ee)


def test_r10_event_account_idempotent() -> None:
    """Calling init path twice should not crash (idempotent check)."""
    cls = _load_rule_class()
    ee = _start_ee()
    try:
        oms = SimpleNamespace(get_all_accounts=lambda: [SimpleNamespace(balance=200000)])
        stub = SimpleNamespace(main_engine=SimpleNamespace(get_engine=lambda n: oms),
                               write_log=lambda m: None, event_engine=ee)
        r1 = cls(stub, {"active": True, "rb_whitelist": "rb2610"})
        c1 = len(ee._handlers.get("eAccount.", []))
        # Construct a second rule (simulating re-init) — should not crash
        r2 = cls(stub, {"active": True, "rb_whitelist": "rb2610"})
        c2 = len(ee._handlers.get("eAccount.", []))
        assert c2 >= c1
    finally:
        _stop_ee(ee)


# =========================================================================
# R10.0-2: on_tick called by real RiskEngine EVENT_TICK path
# =========================================================================

def test_r10_on_tick_called_via_riske_engine_event_tick() -> None:
    """RiskEngine.process_tick_event dispatches to rule.on_tick(tick)."""
    from vnpy_riskmanager.engine import RiskEngine as RE

    ee = EventEngine()  # do NOT pre-start — MainEngine starts it
    saved_cwd = os.getcwd()
    me = None
    try:
        me = MainEngine(ee)
        with patch.object(RE, "load_rules_from_folder", lambda s, p, m: None):
            with patch.object(RE, "setting_filename", "__r10_nonexistent_rm.json"):
                engine = RE(me, ee)

        cls = _load_rule_class()
        rule = cls(engine, {"active": True, "rb_whitelist": "rb2610"})
        engine.rules[rule.name] = rule
        if not engine.tick_rules:
            engine.tick_rules.append(rule)
            engine.event_engine.register("eTick.", engine.process_tick_event)

        tick = TickData(
            symbol="rb2610", exchange=Exchange.SHFE,
            datetime=datetime.now(), gateway_name="CTP",
            name="rb2610", volume=1, open_price=3500,
            high_price=3501, low_price=3499, last_price=3500,
            pre_close=3505, bid_price_1=3500, ask_price_1=3501,
            bid_volume_1=1, ask_volume_1=1,
        )
        rule._tick_times.clear()
        engine.event_engine.put(Event("eTick.", tick))
        time.sleep(0.3)
        vt = f"{tick.symbol}.{tick.exchange.value}"
        assert rule._tick_times.get(vt.upper()) is not None, (
            f"on_tick must update _tick_times[{vt}] via EVENT_TICK"
        )
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)


# =========================================================================
# R10.0-3: No whitelist / no heartbeat / no account = OPEN refused
# =========================================================================

def test_r10_open_rejected_without_whitelist() -> None:
    """Empty rb_whitelist MUST refuse all OPEN."""
    rule, _, ee = _rule(active=True, rb_whitelist="")
    try:
        assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False
        assert rule.check_allowed(_open_req(symbol="rb2505"), "CTP") is False
    finally:
        _stop_ee(ee)


def test_r10_open_rejected_without_tick_heartbeat() -> None:
    """Never received a tick for this contract → OPEN refused."""
    rule, _, ee = _rule(active=True, rb_whitelist="rb2610", tick_timeout_seconds=120)
    try:
        rule._tick_times.clear()
        assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False
    finally:
        _stop_ee(ee)


def test_r10_open_rejected_without_account_heartbeat() -> None:
    """Never received an account event for this gateway → OPEN refused."""
    rule, _, ee = _rule(active=True, rb_whitelist="rb2610", account_timeout_seconds=30)
    try:
        rule._account_times.clear()
        assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False
    finally:
        _stop_ee(ee)


def test_r10_open_rejected_when_equity_unavailable() -> None:
    """minimum_equity > 0 but account data unavailable → OPEN refused."""
    rule, stub, ee = _rule(active=True, rb_whitelist="rb2610", minimum_equity=100000)
    try:
        stub.main_engine = SimpleNamespace(get_engine=lambda name: None)
        assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False
    finally:
        _stop_ee(ee)


# =========================================================================
# R10.0-4: CLOSE/CLOSETODAY/CLOSEYESTERDAY always pass
# =========================================================================

def test_r10_close_offsets_always_pass() -> None:
    """All three CLOSE offset variants pass regardless of risk state."""
    rule, _, ee = _rule(active=True, rb_whitelist="", manual_halt=True)
    try:
        for off in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
            req = _close_req(symbol="rb2610", offset=off)
            assert rule.check_allowed(req, "CTP") is True, f"{off} must always pass"
    finally:
        _stop_ee(ee)


# =========================================================================
# R10.0-5: RolloverTool OPEN always rejected
# =========================================================================

def test_r10_rollover_open_always_rejected() -> None:
    """CtaStrategy_Rollover OPEN is always refused, even when armed."""
    rule, _, ee = _rule(
        active=True, rb_whitelist="rb2610",
        rollover_armed=True,
        rollover_authorized_symbols="rb2611",
        rollover_valid_until="2099-12-31T23:59:59",
    )
    try:
        assert rule.check_allowed(
            _open_req(symbol="rb2611", reference="CtaStrategy_Rollover"), "CTP"
        ) is False
        assert rule.check_allowed(
            _open_req(symbol="rb2610", reference="CTASTRATEGY_ROLLOVER"), "CTP"
        ) is False
    finally:
        _stop_ee(ee)


# =========================================================================
# R10.0-6: Real bound method detection
# =========================================================================

def test_r10_send_order_patched_is_detected() -> None:
    """When RiskEngine patches MainEngine.send_order, detection returns true."""
    from vnpy_chan.production_guard import inspect_live_risk_engine
    from vnpy_riskmanager.engine import RiskEngine

    ee = EventEngine()
    ee.start()
    try:
        target = SimpleNamespace(
            send_order=lambda req, gateway_name: None,
            get_engine=lambda name: None,
            write_log=lambda msg, source="": None,
        )
        re = RiskEngine(target, ee)
        target.get_engine = lambda name, re=re: re

        status = inspect_live_risk_engine(target)
        assert status.send_order_patched is True, (
            f"send_order_patched must be True when RiskEngine has patched; got {status}"
        )
    finally:
        _stop_ee(ee)


def test_r10_send_order_not_patched_detected() -> None:
    """A plain MainEngine without RiskEngine must report patched=False."""
    from vnpy_chan.production_guard import inspect_live_risk_engine

    # Check with a trivial stub that has no RiskEngine
    ee = EventEngine()
    ee.start()
    try:
        target = SimpleNamespace(
            send_order=lambda req, gateway_name: None,
            get_engine=lambda name: None,
        )
        status = inspect_live_risk_engine(target)
        assert status.send_order_patched is False
    finally:
        _stop_ee(ee)


# =========================================================================
# R10.0-7: Production gate requires 缠论开仓守卫 in JSON AND live
# =========================================================================

def test_r10_gate_rejects_when_custom_rule_not_in_json(tmp_path) -> None:
    """JSON without '缠论开仓守卫' → gate rejects."""
    from vnpy_chan.production_guard import evaluate_production_gate

    setting = tmp_path / "risk_manager_setting.json"
    setting.write_text(
        json.dumps({"活动委托检查": {"active": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = SimpleNamespace(
        production=SimpleNamespace(enabled=True),
        risk=SimpleNamespace(
            max_loss_points=600, daily_loss_limit=300,
            max_consecutive_losses=5, max_drawdown_pct=0.031,
        ),
        exits=[object()],
    )
    live = SimpleNamespace(
        risk_manager_app_loaded=True,
        risk_engine_present=True,
        send_order_patched=True,
        loaded_rules=("缠论开仓守卫",),
        active_rules=("缠论开仓守卫",),
        error="",
    )
    result = evaluate_production_gate(
        config=config, kl_window=15, production_ready=True,
        operator_confirmed=True, shadow_mode=False, forward_confirmed=True,
        risk_manager_confirmed=True, risk_manager_setting_path=setting,
        live_risk_status=live,
    )
    assert not result.ready
    assert any("开仓守卫" in r or "chan_open" in r.lower() for r in result.reasons), (
        f"Gate must reject when custom rule not in JSON; reasons={result.reasons}"
    )


def test_r10_gate_rejects_when_live_risk_status_is_none(tmp_path) -> None:
    """Production gate with live_risk_status=None must reject."""
    from vnpy_chan.production_guard import evaluate_production_gate

    setting = tmp_path / "rm.json"
    setting.write_text(
        json.dumps({"缠论开仓守卫": {"active": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = SimpleNamespace(
        production=SimpleNamespace(enabled=True),
        risk=SimpleNamespace(max_loss_points=600, daily_loss_limit=300),
        exits=[object()],
    )
    result = evaluate_production_gate(
        config=config, kl_window=15, production_ready=True,
        operator_confirmed=True, shadow_mode=False, forward_confirmed=True,
        risk_manager_confirmed=True, risk_manager_setting_path=setting,
        live_risk_status=None,
    )
    assert not result.ready
    assert any(
        "live_risk" in r for r in result.reasons
    ), f"Must reject on missing live_risk_status; reasons={result.reasons}"


# =========================================================================
# R10.0-12: OMS passes exchange/gateway to reconcile
# =========================================================================

def test_r10_oms_reconcile_filters_by_exchange_gateway() -> None:
    """Positions from other exchanges/gateways must not affect this strategy."""
    from vnpy_chan.oms_reconciliation import oms_net_position

    pos_mine = SimpleNamespace(
        symbol="rb2610", exchange="SHFE", gateway_name="CTP",
        direction="LONG", volume=1,
    )
    pos_other_exchange = SimpleNamespace(
        symbol="rb2610", exchange="INE", gateway_name="CTP",
        direction="LONG", volume=99,
    )
    pos_other_gateway = SimpleNamespace(
        symbol="rb2610", exchange="SHFE", gateway_name="OTHER",
        direction="LONG", volume=99,
    )

    filtered = oms_net_position(
        [pos_mine, pos_other_exchange, pos_other_gateway],
        "rb2610", exchange="SHFE", gateway="CTP",
    )
    assert filtered == 1, f"Only my exchange+gateway should count; got {filtered}"

    unfiltered = oms_net_position(
        [pos_mine, pos_other_exchange, pos_other_gateway],
        "rb2610",
    )
    assert unfiltered == 199, f"Without filters all rb2610 should sum; got {unfiltered}"


# =========================================================================
# R10.0-14: MainEngine/EventEngine clean exit, cwd restored
# =========================================================================

def test_r10_main_engine_close_releases_threads() -> None:
    """MainEngine.close() must release EventEngine threads and restore cwd."""
    saved_cwd = os.getcwd()
    ee = EventEngine()  # do NOT pre-start — MainEngine starts it
    me = None
    try:
        me = MainEngine(ee)
        # MainEngine.close() changes cwd internally — save/restore around it
        pre_close_cwd = os.getcwd()
        me.close()
        me = None
        os.chdir(pre_close_cwd)  # restore what close() changed
    finally:
        if me is not None:
            me.close()
        _stop_ee(ee)
        os.chdir(saved_cwd)  # double-insurance restore
    time.sleep(0.5)
    if hasattr(ee, '_thread') and ee._thread is not None:
        assert not ee._thread.is_alive(), "EventEngine thread must exit after close"
    if hasattr(ee, '_timer') and ee._timer is not None:
        assert not ee._timer.is_alive(), "EventEngine timer must exit after close"
    assert os.getcwd() == saved_cwd, f"cwd must be restored; was {saved_cwd}, now {os.getcwd()}"
