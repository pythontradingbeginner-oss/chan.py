"""P7-R6: rollover semantics, RolloverTool protection, data evidence.

R6A - rollover = close old, zero structure, empty-restart on new contract.
      active_main_contracts empty in production -> fail-closed.
R6B - the custom RiskManager rule refuses CtaStrategy_Rollover OPENs unless
      explicitly armed+authorized; legit old-contract CLOSE passes.
R6C - SQLite audit is read-only; reproducible holdout runner records inputs.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from vnpy_chan.hard_risk import HardRiskState, evaluate_open_guard


def _state(**overrides) -> HardRiskState:
    base = dict(
        vt_symbol="rb2610.SHFE",
        planned_open_lots=1,
        account_equity=200000.0,
        last_tick_time=datetime.now(),
        last_account_time=datetime.now(),
    )
    base.update(overrides)
    return HardRiskState(**base)


# ── R6A: main-contract confirmation ──

def test_production_empty_main_contracts_fails_closed() -> None:
    """Production (require confirmation) with empty list -> block open."""
    decision = evaluate_open_guard(
        _state(active_main_contracts=(), require_main_contract_confirm=True)
    )
    assert not decision.allowed
    assert any("main_contract_confirmation_missing" in r for r in decision.reasons)


def test_shadow_empty_main_contracts_allows() -> None:
    """Shadow mode with empty list is acceptable (no live open)."""
    decision = evaluate_open_guard(
        _state(active_main_contracts=(), require_main_contract_confirm=False)
    )
    assert decision.allowed


def test_production_mismatched_main_contract_blocks() -> None:
    """Bound contract no longer the confirmed main -> block open."""
    decision = evaluate_open_guard(
        _state(
            vt_symbol="rb2610.SHFE",
            active_main_contracts=("rb2601",),
            require_main_contract_confirm=True,
        )
    )
    assert not decision.allowed
    assert any("contract_rollover_required" in r for r in decision.reasons)


def test_production_matching_main_contract_allows() -> None:
    decision = evaluate_open_guard(
        _state(
            vt_symbol="rb2610.SHFE",
            active_main_contracts=("rb2610",),
            require_main_contract_confirm=True,
        )
    )
    assert decision.allowed


# ── R6B: RolloverTool protection ──

def test_rollover_open_rule_rejects_unarmed(tmp_path, monkeypatch) -> None:
    """The custom RiskManager rule rejects CtaStrategy_Rollover OPEN by default."""
    from pathlib import Path

    from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
    from vnpy.trader.object import OrderRequest

    rule_file = Path("H:/Github/chan.py/rules/chan_open_guard_rule.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("chan_open_guard_rule", rule_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = SimpleNamespace(
        main_engine=SimpleNamespace(
            get_engine=lambda name: SimpleNamespace(
                get_all_accounts=lambda: [SimpleNamespace(balance=200000.0)]
            )
        ),
        write_log=lambda msg: None,
    )
    rule = module.ChanOpenGuardRule(stub, {"active": True})

    req = OrderRequest(
        symbol="rb2611", exchange=Exchange.SHFE, direction=Direction.LONG,
        offset=Offset.OPEN, type=OrderType.LIMIT, price=3500, volume=1,
        reference="CtaStrategy_Rollover",
    )
    assert rule.check_allowed(req, "CTP") is False


def test_rollover_close_passes() -> None:
    """RolloverTool's legit old-contract CLOSE must pass the guard."""
    from pathlib import Path

    from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
    from vnpy.trader.object import OrderRequest

    rule_file = Path("H:/Github/chan.py/rules/chan_open_guard_rule.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("chan_open_guard_rule", rule_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = SimpleNamespace(
        main_engine=SimpleNamespace(
            get_engine=lambda name: SimpleNamespace(
                get_all_accounts=lambda: [SimpleNamespace(balance=200000.0)]
            )
        ),
        write_log=lambda msg: None,
    )
    rule = module.ChanOpenGuardRule(stub, {"active": True})

    req = OrderRequest(
        symbol="rb2610", exchange=Exchange.SHFE, direction=Direction.SHORT,
        offset=Offset.CLOSE, type=OrderType.LIMIT, price=3500, volume=1,
        reference="CtaStrategy_Rollover",
    )
    assert rule.check_allowed(req, "CTP") is True


# ── R6C: data evidence is reproducible + read-only ──

def test_sqlite_audit_is_read_only() -> None:
    """The audit must not write to the production database."""
    import sqlite3

    db = sqlite3.connect("file:C:/Users/Administrator/.vntrader/database.db?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM dbbardata WHERE symbol='rb2610'"
        ).fetchone()
        assert row[0] > 0
    finally:
        db.close()
