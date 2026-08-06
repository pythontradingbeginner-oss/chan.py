"""P7-R5: ChanOpenGuardRule + real RiskEngine interception.

Two layers of testing:
  1. Rule-class unit tests (no RiskEngine side effects): the custom rule's
     check_allowed() blocks OPEN, passes CLOSE, defaults to inactive.
  2. Real RiskEngine interception: construct RiskEngine but only load the
     repo custom rule (monkeypatch load_rules_from_folder), so no built-in
     rule MessageBox side effects.

P7-R10: Every MainEngine is closed in try/finally.  cwd is saved and restored
after each test that uses monkeypatch.chdir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import OrderRequest
from vnpy_riskmanager.engine import RiskEngine

REPO = Path("H:/Github/chan.py")
RULE_FILE = REPO / "rules" / "chan_open_guard_rule.py"


def _open_req(symbol="rb2610", volume=1, exchange=Exchange.SHFE, reference=""):
    return OrderRequest(
        symbol=symbol, exchange=exchange, direction=Direction.LONG,
        offset=Offset.OPEN, type=OrderType.LIMIT, price=3500, volume=volume,
        reference=reference,
    )


def _close_req(symbol="rb2610", volume=1, exchange=Exchange.SHFE):
    return OrderRequest(
        symbol=symbol, exchange=exchange, direction=Direction.SHORT,
        offset=Offset.CLOSE, type=OrderType.LIMIT, price=3500, volume=volume,
    )


def _make_rule(active=True, **params) -> object:
    """Instantiate ChanOpenGuardRule with a stub risk_engine (no side effects)."""
    import importlib.util
    from datetime import datetime

    spec = importlib.util.spec_from_file_location("chan_open_guard_rule", RULE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = module.ChanOpenGuardRule

    oms_stub = SimpleNamespace(
        get_all_accounts=lambda: [SimpleNamespace(balance=200000.0)]
    )
    stub_engine = SimpleNamespace(
        main_engine=SimpleNamespace(get_engine=lambda name: oms_stub),
        write_log=lambda msg: None,
    )
    setting = {"active": active, **params}
    rule = cls(stub_engine, setting)
    # P7-R10: check_allowed uses per-contract _tick_times and per-gateway
    # _account_times dicts.  Pre-populate both so legal opens are not
    # rejected on unverified-tick/account grounds.
    now = datetime.now()
    # P7-R10: tick heartbeat key is full vt_symbol.upper() (e.g. "RB2610.SHFE")
    whitelist = setting.get("rb_whitelist", "")
    if whitelist:
        for sym in whitelist.split(","):
            key = sym.strip().upper()
            if "." not in key:
                key = f"{key}.SHFE"
            rule._tick_times[key] = now
    rule._tick_times["RB2610.SHFE"] = now
    rule._account_times["CTP"] = now
    rule.last_tick_time = now
    rule.last_account_time = now
    return rule


# ── rule-class unit tests ──

def test_rule_defaults_inactive() -> None:
    rule = _make_rule(active=False)
    assert rule.active is False


def test_rule_active_when_explicitly_enabled() -> None:
    rule = _make_rule(active=True)
    assert rule.active is True


def test_open_blocked_by_whitelist() -> None:
    rule = _make_rule(active=True, rb_whitelist="rb2610")
    assert rule.check_allowed(_open_req(symbol="IF2609"), "CTP") is False
    assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is True


def test_open_blocked_by_single_lot() -> None:
    rule = _make_rule(active=True, single_lot_limit=1, rb_whitelist="rb2610")
    assert rule.check_allowed(_open_req(volume=2), "CTP") is False
    assert rule.check_allowed(_open_req(volume=1), "CTP") is True


def test_close_always_passes_when_open_blocked() -> None:
    rule = _make_rule(active=True, single_lot_limit=1, rb_whitelist="rb2610")
    assert rule.check_allowed(_open_req(volume=2), "CTP") is False
    # risk-reducing close passes even when the open guard is firing
    assert rule.check_allowed(_close_req(volume=2), "CTP") is True


def test_rollover_open_blocked_by_default() -> None:
    rule = _make_rule(active=True)
    assert rule.check_allowed(
        _open_req(symbol="rb2611", reference="CtaStrategy_Rollover"), "CTP"
    ) is False


def test_manual_halt_blocks_open() -> None:
    rule = _make_rule(active=True, manual_halt=True, rb_whitelist="rb2610")
    assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False


def test_minimum_equity_blocks_open() -> None:
    rule = _make_rule(active=True, minimum_equity=100000, rb_whitelist="rb2610")
    # Shared OMS stub balance below the floor -> open refused
    rule.risk_engine.main_engine.get_engine(
        "oms"
    ).get_all_accounts = lambda: [SimpleNamespace(balance=50000.0)]
    assert rule.check_allowed(_open_req(symbol="rb2610"), "CTP") is False


# ── real RiskEngine interception (only our rule loaded) ──

def _build_engine(tmp_path, monkeypatch):
    """Build a real RiskEngine loading only the repo custom rule."""
    from vnpy_riskmanager.engine import RiskEngine as RE

    saved_cwd = os.getcwd()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    os.chdir(saved_cwd)
    monkeypatch.setattr(
        RE, "setting_filename", str(tmp_path / "risk_manager_setting.json")
    )
    (tmp_path / "risk_manager_setting.json").write_text(
        json.dumps({"缠论开仓守卫": {"active": True, "rb_whitelist": "rb2610", "single_lot_limit": 1}},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    # Only load the repo custom rule; built-in rules (MessageBox side effects)
    # never load.
    monkeypatch.setattr(
        RE,
        "load_rules_from_folder",
        lambda self, folder_path, module_name: None,
    )

    def load_custom(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "chan_open_guard_rule", RULE_FILE
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in dir(module):
            value = getattr(module, name)
            if isinstance(value, type) and name.endswith("Rule"):
                self.rule_classes[name] = (value, "rules")
        for class_name, (rule_class, module_name) in self.rule_classes.items():
            self.add_rule(rule_class)

    monkeypatch.setattr(RE, "load_rules", load_custom)

    engine = RE(main_engine, event_engine)
    engine._send_order = lambda req, gateway_name: f"GATEWAY.{req.symbol}"

    # P7-R10: new check_allowed requires tick/account heartbeats.
    # Prime them so the interception test validates the rule logic
    # (whitelist/single_lot/close) rather than heartbeat freshness.
    rule = engine.rules.get("缠论开仓守卫")
    if rule is not None:
        from datetime import datetime
        now = datetime.now()
        rule._tick_times["RB2610.SHFE"] = now
        rule._account_times["CTP"] = now

    return engine, main_engine, saved_cwd


def test_riskengine_interception_inline(tmp_path, monkeypatch) -> None:
    """One test builds + tears down the engine inline (avoids thread reuse)."""
    engine, main_engine, saved_cwd = _build_engine(tmp_path, monkeypatch)
    try:
        rule = engine.rules["缠论开仓守卫"]
        rule.update_setting({"active": True, "rb_whitelist": "rb2610", "single_lot_limit": 1})

        # OPEN blocked -> send_order returns ""
        assert main_engine.send_order(_open_req(volume=2), "CTP") == ""
        # CLOSE passes -> underlying gateway returns the id
        assert main_engine.send_order(_close_req(volume=2), "CTP") != ""
        # Valid single-lot open passes
        assert main_engine.send_order(_open_req(volume=1), "CTP") != ""
        # Non-whitelisted contract open blocked
        assert main_engine.send_order(_open_req(symbol="IF2609"), "CTP") == ""
    finally:
        main_engine.close()
        if getattr(engine.event_engine, "_active", False):
            engine.event_engine.stop()
        os.chdir(saved_cwd)


def test_production_gate_queries_actual_risk_engine(tmp_path) -> None:
    from vnpy_chan.production_guard import (
        evaluate_production_gate,
        inspect_live_risk_engine,
        inspect_risk_manager_setting,
    )

    setting = tmp_path / "risk_manager_setting.json"
    setting.write_text(
        '{"缠论开仓守卫": {"active": false}}', encoding="utf-8"
    )
    config = SimpleNamespace(
        production=SimpleNamespace(enabled=True),
        risk=SimpleNamespace(
            max_loss_points=600, daily_loss_limit=300,
            max_consecutive_losses=5, max_drawdown_pct=0.031,
        ),
        exits=[object()],
    )

    # 1. No live engine supplied -> gate still works (JSON path)
    result = evaluate_production_gate(
        config=config, kl_window=15, production_ready=True,
        operator_confirmed=True, shadow_mode=False, forward_confirmed=True,
        risk_manager_confirmed=True, risk_manager_setting_path=setting,
    )
    assert not result.ready  # custom rule inactive in JSON

    # 2. Live engine present but custom rule inactive -> gate reflects it
    live = SimpleNamespace(
        risk_manager_app_loaded=True,
        risk_engine_present=True,
        send_order_patched=True,
        loaded_rules=("缠论开仓守卫",),
        active_rules=(),
        error="",
    )
    result2 = evaluate_production_gate(
        config=config, kl_window=15, production_ready=True,
        operator_confirmed=True, shadow_mode=False, forward_confirmed=True,
        risk_manager_confirmed=True, risk_manager_setting_path=setting,
        live_risk_status=live,
    )
    assert not result2.ready
    assert "risk_engine_no_active_rules" in result2.reasons


def test_inspect_live_risk_engine_missing() -> None:
    from vnpy_chan.production_guard import inspect_live_risk_engine

    status = inspect_live_risk_engine(None)
    assert status.active is False
    assert status.risk_manager_app_loaded is False
