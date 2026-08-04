from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from vnpy_chan.hard_risk import HardRiskState, evaluate_open_guard


def _state(**overrides) -> HardRiskState:
    values = dict(
        vt_symbol="rb2610.SHFE",
        planned_open_lots=1,
        manual_halt=False,
        minimum_equity=0.0,
        account_equity=100000.0,
        last_tick_time=datetime.now(),
        last_account_time=datetime.now(),
        tick_timeout_seconds=120.0,
        account_timeout_seconds=30.0,
        realized_day_loss=None,
        daily_loss_limit=None,
        drawdown_pct=None,
        max_drawdown_pct=None,
    )
    values.update(overrides)
    return HardRiskState(**values)


def test_clean_state_allows_open() -> None:
    decision = evaluate_open_guard(_state())
    assert decision.allowed
    assert decision.reasons == ()


def test_non_rb_symbol_blocked() -> None:
    decision = evaluate_open_guard(_state(vt_symbol="IF2609.CFFEX"))
    assert not decision.allowed
    assert any("rb_contract_whitelist" in r for r in decision.reasons)


def test_multi_lot_open_blocked() -> None:
    decision = evaluate_open_guard(_state(planned_open_lots=2))
    assert not decision.allowed
    assert any("single_lot_limit" in r for r in decision.reasons)


def test_manual_halt_blocks_open() -> None:
    decision = evaluate_open_guard(_state(manual_halt=True))
    assert not decision.allowed
    assert "manual_halt" in decision.reasons


def test_minimum_equity_blocks_open() -> None:
    decision = evaluate_open_guard(
        _state(minimum_equity=50000.0, account_equity=40000.0)
    )
    assert not decision.allowed
    assert any("minimum_equity" in r for r in decision.reasons)


def test_stale_tick_blocks_open() -> None:
    decision = evaluate_open_guard(
        _state(
            last_tick_time=datetime.now() - timedelta(seconds=300),
            last_account_time=datetime.now(),
        )
    )
    assert not decision.allowed
    assert any("market_heartbeat_timeout" in r for r in decision.reasons)


def test_stale_account_blocks_open() -> None:
    decision = evaluate_open_guard(
        _state(
            last_tick_time=datetime.now(),
            last_account_time=datetime.now() - timedelta(seconds=120),
        )
    )
    assert not decision.allowed
    assert any("account_heartbeat_timeout" in r for r in decision.reasons)


def test_daily_loss_limit_blocks_open() -> None:
    decision = evaluate_open_guard(
        _state(realized_day_loss=-310.0, daily_loss_limit=300.0)
    )
    assert not decision.allowed
    assert any("daily_loss_limit" in r for r in decision.reasons)


def test_daily_loss_within_limit_allows() -> None:
    decision = evaluate_open_guard(
        _state(realized_day_loss=-200.0, daily_loss_limit=300.0)
    )
    assert decision.allowed


def test_max_drawdown_blocks_open() -> None:
    decision = evaluate_open_guard(
        _state(drawdown_pct=0.035, max_drawdown_pct=0.031)
    )
    assert not decision.allowed
    assert any("max_drawdown" in r for r in decision.reasons)


def test_close_is_never_blocked_by_hard_risk() -> None:
    # The guard is only evaluated for opens; closes bypass it entirely.
    # Verify the strategy sends a close even under manual halt.
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.shadow_mode = False
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.manual_halt = True
    strategy.minimum_equity = 0.0
    strategy._recovery_required = False
    strategy._last_tick_time = datetime.now()
    strategy._last_account_time = datetime.now()
    strategy.write_log = lambda message: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="",
    )
    strategy._evaluate_hard_risk = lambda lots: SimpleNamespace(
        allowed=False,
        reason_text="manual_halt",
    )
    called = []

    def sender():
        called.append(True)
        return ["SIM.1"]

    result = strategy._send_live_or_shadow(
        "close",
        sender,
        price=3500,
        volume=1,
        closing=True,
        reason="stop",
    )

    assert result == ["SIM.1"]
    assert called


def test_open_blocked_by_hard_risk_in_shadow() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.shadow_mode = True
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.manual_halt = True
    strategy.minimum_equity = 0.0
    strategy._recovery_required = False
    strategy.hard_risk_status = "armed"
    strategy.hard_risk_reasons = ""
    strategy._last_tick_time = datetime.now()
    strategy._last_account_time = datetime.now()
    strategy.write_log = lambda message: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="shadow_mode_enabled",
    )
    strategy._evaluate_hard_risk = lambda lots: SimpleNamespace(
        allowed=False,
        reason_text="manual_halt",
    )
    called = []

    def sender():
        called.append(True)
        return ["SIM.1"]

    result = strategy._send_live_or_shadow(
        "open_long",
        sender,
        price=3500,
        volume=1,
        closing=False,
        reason="entry",
    )

    assert result == []
    assert not called
    assert strategy.order_status == "hard_risk_blocked"
    assert strategy.hard_risk_status == "BLOCKED"
