from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from chan_futures.risk import RiskConfig, RiskManager
from chan_futures.risk_session import RbRiskSessionResolver
from chan_futures.parity import compare_runtime_traces
from chan_futures.strategy import StrategySignal
from chan_futures.trade_intent import TradeIntent
from data_foundation import CalendarCoverageError
from signal_core.models import SignalDecision, SignalDirection


def _open_signal():
    return SimpleNamespace(target_position=1)


def test_daily_loss_recovers_on_next_trading_session_only() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100))
    risk.advance_session("2025-01-06", observed_at="2025-01-03 21:01", source="bar")
    risk.on_fill(
        pnl_points=-100,
        fill_time="2025-01-06 10:00",
        session_key="2025-01-06",
    )

    assert not risk.approve(_open_signal()).approved
    assert not risk.advance_session(
        "2025-01-06",
        observed_at="2025-01-06 14:59",
        source="bar",
    )
    assert risk.get_state()["daily_realized"] == -100

    assert risk.advance_session(
        "2025-01-07",
        observed_at="2025-01-06 21:01",
        source="bar",
    )
    assert risk.get_state()["daily_realized"] == 0
    assert risk.approve(_open_signal()).approved


def test_session_advance_does_not_reset_consecutive_losses() -> None:
    risk = RiskManager(
        RiskConfig(daily_loss_limit=100, max_consecutive_losses=2)
    )
    risk.on_fill(
        pnl_points=-10,
        fill_time="2025-01-06 10:00",
        session_key="2025-01-06",
    )

    risk.advance_session("2025-01-07", observed_at="2025-01-06 21:01")

    assert risk.get_state()["consecutive_losses"] == 1


def test_fill_can_advance_new_session_before_first_bar() -> None:
    risk = RiskManager(
        RiskConfig(daily_loss_limit=100, require_session_key=True)
    )
    risk.on_fill(
        pnl_points=-100,
        fill_time="2025-01-06 14:59",
        session_key="2025-01-06",
    )

    risk.on_fill(
        pnl_points=-10,
        fill_time="2025-01-06 21:01",
        session_key="2025-01-07",
    )

    state = risk.get_state()
    assert state["current_session_key"] == "2025-01-07"
    assert state["daily_realized"] == -10
    assert state["consecutive_losses"] == 2


def test_formal_rb_risk_fails_closed_without_session() -> None:
    risk = RiskManager(
        RiskConfig(daily_loss_limit=100, require_session_key=True)
    )

    assert risk.approve(_open_signal()).reason == "risk_session_uninitialized"
    with pytest.raises(ValueError, match="risk_session_key_required"):
        risk.on_fill(pnl_points=-10, fill_time="2025-01-06 10:00")


def test_regressing_fill_is_rejected_before_risk_state_mutates() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100))
    risk.advance_session("2025-01-07", observed_at="2025-01-06 21:01")
    before = risk.get_state()

    with pytest.raises(ValueError, match="risk_session_regression"):
        risk.on_fill(
            pnl_points=-20,
            fill_time="2025-01-06 14:59",
            session_key="2025-01-06",
        )

    after = risk.get_state()
    assert after["total_fills"] == before["total_fills"]
    assert after["realized_points"] == before["realized_points"]
    assert after["daily_realized"] == before["daily_realized"]


def test_restored_session_is_idempotent_and_keeps_daily_loss() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100))
    risk.load_state(
        {
            "current_session_key": "2025-01-06",
            "last_session_observed_at": "2025-01-06T14:59:00",
            "session_source": "persisted",
            "daily_realized": -40,
        }
    )

    changed = risk.advance_session(
        "2025-01-06",
        observed_at="2025-01-06 09:00",
        source="replay",
    )

    assert not changed
    assert risk.get_state()["daily_realized"] == -40
    assert risk.get_state()["last_session_observed_at"] == "2025-01-06T14:59:00"


def test_rb_resolver_assigns_friday_night_to_monday_trading_day() -> None:
    context = RbRiskSessionResolver().resolve(
        "2025-01-03 21:01:37",
        source="fill",
    )

    assert context.session_key == "2025-01-06"
    assert context.market_session == "NIGHT"


def test_rb_resolver_rejects_explicit_trading_day_mismatch() -> None:
    with pytest.raises(CalendarCoverageError, match="risk_session_mismatch"):
        RbRiskSessionResolver().resolve(
            "2025-01-03 21:01",
            source="backtest_bar",
            explicit_trading_day="2025-01-03",
        )


def test_equity_sequence_has_runtime_parity_and_oms_rebases_fallback() -> None:
    config = RiskConfig(max_drawdown_pct=0.04)
    backtest = RiskManager(
        config,
        initial_equity=100_000,
        initial_equity_source="backtest_mark_to_market",
        equity_scope="simulated_account",
        max_drawdown_capability="enabled",
    )
    cta = RiskManager(
        config,
        initial_equity=1_000_000,
        initial_equity_source="config_fallback",
        equity_scope="oms_all_accounts",
        max_drawdown_capability="enabled",
    )

    cta.observe_equity(100_000, source="oms_balance", scope="oms_all_accounts")
    for value in (102_000, 110_000, 105_000):
        backtest.observe_equity(value, source="backtest_mark_to_market")
        cta.observe_equity(value, source="oms_balance")

    assert backtest.get_state()["peak_equity"] == 110_000
    assert cta.get_state()["peak_equity"] == 110_000
    assert backtest.approve(_open_signal(), current_equity=105_000) == cta.approve(
        _open_signal(), current_equity=105_000
    )
    assert cta.get_state()["equity_source"] == "oms_balance"


def test_drawdown_dependent_approval_rejects_invalid_equity() -> None:
    risk = RiskManager(RiskConfig(max_drawdown_pct=0.10), initial_equity=100_000)

    assert risk.approve(_open_signal(), current_equity=float("nan")).reason == (
        "account_equity_unavailable"
    )


def test_setup_candidate_is_stable_and_observation_only() -> None:
    decided_at = datetime(2025, 1, 6, 10, 0)
    event = SimpleNamespace(
        event_id="event-r0",
        signal_key="signal-1",
        direction=SignalDirection.LONG,
        primary_bsp="2",
        bi_idx=20,
        related_bsp1_bi_idx=12,
        contract="RB_MAIN",
        timeframe="15m",
        bi_begin_price=3490,
        zs_high=3520,
        zs_low=3480,
    )
    signal = StrategySignal(
        timestamp=decided_at,
        action="open_long",
        target_position=1,
        price=3500,
        reason="chan_bsp",
        bsp_type="2,3b",
        bsp_bi_idx=20,
        bsp_klu_idx=100,
        active_symbol="RB2505",
    )
    decision = SignalDecision(
        decision_id="decision-1",
        event_id=event.event_id,
        policy_id="test",
        accepted=True,
        reason_codes=(),
        entry_price_hint=3500,
        invalidation_price=3480,
        initial_stop_price=3485,
        position_size_hint=1,
        decided_at=decided_at,
    )
    first = TradeIntent(signal=signal, decision=decision, event=event)
    revision = TradeIntent(
        signal=signal,
        decision=decision,
        event=SimpleNamespace(**{**event.__dict__, "event_id": "event-r1"}),
    )

    first_trace = first.to_trace_record(risk_session_key="2025-01-06")
    revision_trace = revision.to_trace_record(risk_session_key="2025-01-06")
    assert first_trace.setup_candidate_id == revision_trace.setup_candidate_id
    assert first_trace.setup_root_bi_idx == 12
    assert first_trace.setup_state == "observed_only"
    assert first_trace.raw_bi_idx == 20
    assert first_trace.raw_klu_idx == 100
    assert first_trace.raw_bsp_type == "2,3b"
    assert first.position_from_fill(
        fill_price=3501,
        fill_volume=1,
        fill_time=decided_at,
    ).setup_candidate_id == first_trace.setup_candidate_id

    next_contract = TradeIntent(
        signal=SimpleNamespace(**{**signal.__dict__, "active_symbol": "RB2510"}),
        decision=decision,
        event=event,
    )
    assert next_contract.setup_candidate.candidate_id != first.setup_candidate.candidate_id

    provenance_variant = replace(
        first_trace,
        risk_session_source="cta_bar",
        equity_source="oms_balance",
        equity_scope="oms_all_accounts",
    )
    assert compare_runtime_traces(
        {"backtest": [first_trace], "cta": [provenance_variant]}
    ).consistent
    session_variant = replace(provenance_variant, risk_session_key="2025-01-07")
    assert not compare_runtime_traces(
        {"backtest": [first_trace], "cta": [session_variant]}
    ).consistent


def test_p0_audit_schema_and_rule_contract_are_valid_json() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "configs" / "qingpai_p0_audit_schema_v1.json").read_text(
            encoding="utf-8"
        )
    )
    contract = json.loads(
        (root / "configs" / "qingpai_rule_contract_v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["properties"]["schema_version"]["const"] == "qingpai-p0-audit-v1"
    assert {item["id"] for item in contract["open_decisions"]} >= {
        "OPEN-005",
        "OPEN-006",
    }
