from __future__ import annotations

import pandas as pd

from scripts.audit_qingpai_mechanisms import (
    _codes_to_text,
    _momentum_scope,
    _regime_source,
    _setup_anchor_type,
    _stop_anchor_type,
    add_execution_status,
    build_decision_audit,
    sample_rejected_decisions,
)


def test_reason_code_parser_handles_tuple_text_and_pipe_text() -> None:
    assert _codes_to_text(("a", "b")) == "a|b"
    assert _codes_to_text("('parent_direction_conflict', 'missing')") == (
        "parent_direction_conflict|missing"
    )
    assert _codes_to_text("already|flat") == "already|flat"


def test_regime_source_splits_missing_decomposition_from_explicit_unclassified() -> None:
    assert _regime_source("unclassified", False) == "no_decomposition_snapshot"
    assert _regime_source("unclassified", True) == "explicit_unclassified"
    assert _regime_source("trend", True) == "classified"


def test_momentum_scope_marks_t2_as_coverage_gap_not_code_failure() -> None:
    assert _momentum_scope("1p") == "applies_t1"
    assert _momentum_scope("2s") == "not_applicable_t2_coverage_gap"
    assert _momentum_scope("3a") == "not_applicable_t3_coverage_gap"


def test_anchor_types_follow_qingpai_level_semantics() -> None:
    t2 = {"primary_bsp": "2", "direction": "long"}
    t3_short = {"primary_bsp": "3b", "direction": "short"}
    assert _stop_anchor_type(t2) == "t2_retrace_structural_price"
    assert _setup_anchor_type(t2) == "related_bsp1_price"
    assert _stop_anchor_type(t3_short) == "t3_pullback_structural_price"
    assert _setup_anchor_type(t3_short) == "zs_low"


def test_rejected_decision_sampling_is_deterministic_by_stratum() -> None:
    decisions = pd.DataFrame(
        [
            {
                "timestamp": f"2026-01-01 09:{i:02d}:00+08:00",
                "accepted": False,
                "direction": "short" if i % 2 else "long",
                "bsp_type": "2",
                "primary_reason": "parent_direction_conflict" if i % 2 else "missing",
                "regime_source": "classified",
            }
            for i in range(10)
        ]
    )
    first = sample_rejected_decisions(decisions, per_stratum=2, max_rows=10, seed=7)
    second = sample_rejected_decisions(decisions, per_stratum=2, max_rows=10, seed=7)
    pd.testing.assert_frame_equal(first.reset_index(drop=True), second.reset_index(drop=True))
    assert len(first) == 4


def test_decision_audit_marks_time_integrity_issue() -> None:
    trace = pd.DataFrame(
        [
            {
                "timestamp": "2026-01-01 10:00:00+08:00",
                "event_id": "E1",
                "accepted": False,
                "reason_codes": ("parent_direction_conflict",),
                "direction": "short",
                "bsp_type": "2s",
                "regime": "unclassified",
                "decomposition_id": "",
                "parent_available_at": "2026-01-01 10:15:00+08:00",
                "momentum_available_at": "2026-01-01 10:00:00+08:00",
                "multi_level_time_honest": True,
            }
        ]
    )
    events = pd.DataFrame([{"event_id": "E1", "bar_end_time": "2026-01-01 10:00:00+08:00"}])
    result = build_decision_audit(trace, events, [])
    assert bool(result.loc[0, "time_integrity_issue"]) is True
    assert result.loc[0, "regime_source"] == "no_decomposition_snapshot"


def test_execution_status_distinguishes_accepted_from_executed() -> None:
    decisions = pd.DataFrame(
        [
            {"event_id": "E1", "accepted": True},
            {"event_id": "E2", "accepted": True},
            {"event_id": "E3", "accepted": False},
        ]
    )
    accepted = pd.DataFrame([{"event_id": "E1"}])
    execution_decisions = pd.DataFrame(
        [
            {
                "event_id": "E2",
                "status": "risk_rejected",
                "rejection_stage": "risk",
                "rejection_reason": "max_consecutive_losses: 3 >= 3",
                "risk_approved": False,
                "risk_reason": "max_consecutive_losses: 3 >= 3",
                "risk_realized_points": -48.0,
                "risk_consecutive_losses": 3,
            }
        ]
    )

    result = add_execution_status(decisions, accepted, execution_decisions)

    assert result.loc[0, "execution_status"] == "accepted_executed_trade"
    assert result.loc[1, "execution_status"] == "accepted_not_executed"
    assert result.loc[1, "execution_disposition"] == "risk_rejected"
    assert result.loc[1, "execution_rejection_stage"] == "risk"
    assert result.loc[1, "risk_consecutive_losses"] == 3
    assert result.loc[2, "execution_status"] == "rejected_by_entry_or_context"
