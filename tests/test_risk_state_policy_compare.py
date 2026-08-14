from __future__ import annotations

import pandas as pd

from scripts.compare_risk_state_policies import (
    audit_decision_parity,
    filter_source_frames,
    summarize_account_drawdown,
    summarize_policy_frames,
)


def test_summarize_policy_frames_separates_risk_rejected_from_executed() -> None:
    frames = {
        "decision_trace": pd.DataFrame(
            [
                {"event_id": "E1", "accepted": True, "direction": "long", "timestamp": "2026-01-01"},
                {"event_id": "E2", "accepted": True, "direction": "short", "timestamp": "2026-01-02"},
                {"event_id": "E3", "accepted": False, "direction": "short", "timestamp": "2026-01-03"},
            ]
        ),
        "execution_decisions": pd.DataFrame(
            [
                {"event_id": "E1", "status": "executed_open", "timestamp": "2026-01-01"},
                {
                    "event_id": "E2",
                    "status": "risk_rejected",
                    "rejection_reason": "max_consecutive_losses: 5 >= 5",
                    "timestamp": "2026-01-02",
                },
            ]
        ),
        "trades": pd.DataFrame(
            [{"event_id": "E1", "direction": "long", "pnl_points": 7, "entry_time": "2026-01-01"}]
        ),
    }

    result = summarize_policy_frames("case", frames)

    assert result["decision_count"] == 3
    assert result["accepted_count"] == 2
    assert result["trade_count"] == 1
    assert result["accepted_not_executed_count"] == 1
    assert result["risk_rejected_count"] == 1
    assert "max_consecutive_losses" in result["risk_rejection_reasons"]


def test_filter_source_frames_uses_fold_test_windows() -> None:
    frames = {
        "decision_trace": pd.DataFrame(
            [
                {"timestamp": "2026-01-01", "event_id": "before"},
                {"timestamp": "2026-02-01", "event_id": "inside"},
                {"timestamp": "2026-03-01", "event_id": "after"},
            ]
        ),
        "execution_decisions": pd.DataFrame(
            [{"timestamp": "2026-02-15", "event_id": "inside"}]
        ),
        "trades": pd.DataFrame(
            [{"entry_time": "2026-02-20", "event_id": "inside"}]
        ),
    }
    folds = [
        {
            "test_start": pd.Timestamp("2026-02-01"),
            "test_end": pd.Timestamp("2026-03-01"),
        }
    ]

    result = filter_source_frames(frames, folds)

    assert list(result["decision_trace"]["event_id"]) == ["inside"]
    assert len(result["execution_decisions"]) == 1
    assert len(result["trades"]) == 1


def test_audit_decision_parity_reports_missing_accepted_event() -> None:
    continuous = {
        "decision_trace": pd.DataFrame(
            [
                {"timestamp": "2026-01-01", "event_id": "shared", "accepted": False},
                {"timestamp": "2026-01-02", "event_id": "missing", "accepted": True},
            ]
        )
    }
    fold_reset = {
        "decision_trace": pd.DataFrame(
            [{"timestamp": "2026-01-01", "event_id": "shared", "accepted": False}]
        )
    }

    summary, diff = audit_decision_parity(continuous, fold_reset)

    assert not summary["exact"]
    assert summary["shared_count"] == 1
    assert summary["continuous_only_count"] == 1
    assert summary["accepted_continuous_only_count"] == 1
    assert list(diff["parity_issue"]) == ["continuous_only"]


def test_summarize_account_drawdown_uses_running_account_peak() -> None:
    bars = pd.DataFrame({"account_equity": [100000, 102000, 99000, 101000]})

    summary = summarize_account_drawdown(bars, threshold=0.02)

    assert summary["max_account_drawdown_pct"] == (102000 - 99000) / 102000
    assert summary["drawdown_breach_bar_count"] == 1
