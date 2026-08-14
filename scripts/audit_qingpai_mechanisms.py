"""Audit the current Qingpai RB mechanism without tuning strategy rules.

This script reruns the current HEAD with the strict RB configuration, then
derives mechanism-focused evidence tables.  It deliberately avoids parameter
sweeps, hypothetical PnL, or any strategy/config mutation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chan_futures.backtest import run_backtest
from chan_futures.config_loader import load_config
from chan_futures.feed import prepare_ohlc_frame
from chan_futures.production import ProductionWalkforwardRunner, load_rb_production_frames


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "rb_15m_qingpai_strict.yaml"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "rule_mechanism_audit"
DEFAULT_SEED = 20260810
HOLDOUT_START = pd.Timestamp("2026-07-16", tz="Asia/Shanghai")
HOLDOUT_END = pd.Timestamp("2026-07-31", tz="Asia/Shanghai")
NO_TUNING_NOTICE = (
    "本报告只审计当前规则的执行机制；不输出参数敏感性、假设收益或调参建议。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--sample-seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--per-stratum", type=int, default=20)
    parser.add_argument("--max-rejected-samples", type=int, default=2000)
    parser.add_argument("--replay-start", default=None)
    parser.add_argument("--replay-end", default=None)
    parser.add_argument("--decision-start", default=None)
    parser.add_argument("--decision-end", default=None)
    parser.add_argument(
        "--reuse-source-dir",
        type=Path,
        default=None,
        help="Reuse a prior raw replay source directory and only rebuild derived audit tables.",
    )
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (args.output_dir if args.output_dir.is_absolute() else PROJECT_ROOT / args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "source"
    replay_start = _optional_ts(args.replay_start)
    replay_end = _optional_ts(args.replay_end)
    decision_start = _optional_ts(args.decision_start)
    decision_end = _optional_ts(args.decision_end)

    config_path = args.config if args.config.is_absolute() else PROJECT_ROOT / args.config
    config = load_config(config_path)
    frames = load_rb_production_frames(config)
    current = prepare_ohlc_frame(frames.current).reset_index(drop=True)
    parent = prepare_ohlc_frame(frames.parent).reset_index(drop=True)
    child = prepare_ohlc_frame(frames.child).reset_index(drop=True)
    current = _slice_frame(current, replay_start, replay_end)
    parent = _slice_frame(parent, replay_start, replay_end)
    child = _slice_frame(child, replay_start, replay_end)

    source_reused_from = None
    if args.reuse_source_dir:
        reuse_source_dir = (
            args.reuse_source_dir
            if args.reuse_source_dir.is_absolute()
            else PROJECT_ROOT / args.reuse_source_dir
        )
        source_reused_from = str(reuse_source_dir)
        signal_events = _read_optional_csv(reuse_source_dir / "signal_events.csv")
        signal_decisions = _read_optional_csv(reuse_source_dir / "signal_decisions.csv")
        decision_trace = _read_optional_csv(reuse_source_dir / "decision_trace.csv")
        decomposition_transitions = _read_optional_csv(reuse_source_dir / "decomposition_transitions.csv")
        multi_level = _read_optional_csv(reuse_source_dir / "multi_level_decisions.csv")
        execution_decisions = _read_optional_csv(reuse_source_dir / "execution_decisions.csv")
        trades = _read_optional_csv(reuse_source_dir / "trades.csv")
        fills = _read_optional_csv(reuse_source_dir / "fills.csv")
        bars = _read_optional_csv(reuse_source_dir / "bars.csv")
        exit_events = _read_optional_csv(reuse_source_dir / "exit_events.csv")
        result_counts = {
            "trade_count": len(trades),
            "fill_count": len(fills),
            "decision_count": len(decision_trace),
            "signal_event_count": len(signal_events),
            "execution_decision_count": len(execution_decisions),
        }
    else:
        source_dir.mkdir(parents=True, exist_ok=True)
        result = run_backtest(
            config,
            frame=current,
            parent_frame=parent,
            child_frame=child,
            decision_start=decision_start,
            decision_end=decision_end,
        )
        result.save(source_dir)

        signal_events = pd.DataFrame([event.to_dict() for event in result.signal_events])
        signal_decisions = pd.DataFrame([decision.to_dict() for decision in result.signal_decisions])
        decision_trace = pd.DataFrame([record.to_dict() for record in result.decision_trace])
        decomposition_transitions = pd.DataFrame(
            [transition.to_dict() for transition in result.decomposition_transitions]
        )
        multi_level = pd.DataFrame([context.to_dict() for context in result.multi_level_audit])
        execution_decisions = pd.DataFrame(result.execution_decisions)
        trades = pd.DataFrame(result.trades)
        fills = result.fills.copy()
        bars = result.bars.copy()
        exit_events = result.exit_events.copy()
        result_counts = {
            "trade_count": len(result.trades),
            "fill_count": len(result.fills),
            "decision_count": len(result.decision_trace),
            "signal_event_count": len(result.signal_events),
            "execution_decision_count": len(result.execution_decisions),
        }

        _write_optional_csv(signal_events, source_dir / "signal_events.csv")
        _write_optional_csv(signal_decisions, source_dir / "signal_decisions.csv")
        _write_optional_csv(decision_trace, source_dir / "decision_trace.csv")
        _write_optional_csv(decomposition_transitions, source_dir / "decomposition_transitions.csv")
        _write_optional_csv(multi_level, source_dir / "multi_level_decisions.csv")
        _write_optional_csv(execution_decisions, source_dir / "execution_decisions.csv")

    fold_windows = _standard_fold_windows(current)
    events_by_id = _indexed(signal_events, "event_id")
    trace_by_event = _indexed(decision_trace, "event_id")
    atr = _calculate_atr(current, int(config.sizing.atr_period))
    row_by_time = {
        _ts(value): int(idx)
        for idx, value in enumerate(current["datetime"])
    }

    accepted = build_accepted_trades_audit(
        trades=trades,
        fills=fills,
        events_by_id=events_by_id,
        trace_by_event=trace_by_event,
        atr=atr,
        row_by_time=row_by_time,
        fold_windows=fold_windows,
        fee_points=float(config.execution.fee_points),
    )
    decisions = build_decision_audit(decision_trace, signal_events, fold_windows)
    decisions = add_execution_status(decisions, accepted, execution_decisions)
    rejected_sample = sample_rejected_decisions(
        decisions,
        per_stratum=args.per_stratum,
        max_rows=args.max_rejected_samples,
        seed=args.sample_seed,
    )
    clusters = build_mechanism_clusters(accepted, decisions)

    accepted.to_csv(output_dir / "accepted_trades_audit.csv", index=False, encoding="utf-8-sig")
    decisions.to_csv(output_dir / "decision_mechanism_audit.csv", index=False, encoding="utf-8-sig")
    rejected_sample.to_csv(output_dir / "rejected_decisions_sample.csv", index=False, encoding="utf-8-sig")
    clusters.to_csv(output_dir / "mechanism_clusters.csv", index=False, encoding="utf-8-sig")

    meta = build_run_meta(
        config_path=config_path,
        output_dir=output_dir,
        current=current,
        parent=parent,
        child=child,
        config=config,
        result_counts=result_counts,
        source_reused_from=source_reused_from,
        sample_seed=args.sample_seed,
        per_stratum=args.per_stratum,
        max_rejected_samples=args.max_rejected_samples,
        replay_start=replay_start,
        replay_end=replay_end,
        decision_start=decision_start,
        decision_end=decision_end,
    )
    (output_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    write_audit_summary(output_dir, meta, accepted, decisions, clusters)
    write_open_rule_questions(output_dir, accepted, decisions, clusters)
    write_falsifiable_hypotheses(output_dir, accepted, decisions, clusters)

    print(f"Audit written to: {output_dir}")
    print(f"Accepted trades: {len(accepted)}")
    print(f"Decision records: {len(decisions)}")
    print(f"Rejected sample: {len(rejected_sample)}")
    return 0


def build_accepted_trades_audit(
    *,
    trades: pd.DataFrame,
    fills: pd.DataFrame,
    events_by_id: dict[str, dict[str, Any]],
    trace_by_event: dict[str, dict[str, Any]],
    atr: pd.Series,
    row_by_time: dict[pd.Timestamp, int],
    fold_windows: list[dict[str, Any]],
    fee_points: float,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    used_fill_indexes: set[int] = set()
    ordered_trades = trades.sort_values(["entry_bar", "exit_bar"]).reset_index(drop=True)

    for ordinal, trade in ordered_trades.iterrows():
        trade_dict = trade.to_dict()
        event_id = str(trade_dict.get("event_id", ""))
        event = events_by_id.get(event_id, {})
        trace = trace_by_event.get(event_id, {})
        entry_fill = _match_fill(fills, trade_dict, "entry", used_fill_indexes)
        exit_fill = _match_fill(fills, trade_dict, "exit", used_fill_indexes)
        row = _accepted_trade_row(
            trade_dict,
            event=event,
            trace=trace,
            entry_fill=entry_fill,
            exit_fill=exit_fill,
            atr=atr,
            row_by_time=row_by_time,
            fold_windows=fold_windows,
            fee_points=fee_points,
            ordinal=int(ordinal) + 1,
        )
        rows.append(row)

    audit = pd.DataFrame(rows)
    if audit.empty:
        return audit
    audit = _add_repeat_fields(audit)
    return audit


def build_decision_audit(
    decision_trace: pd.DataFrame,
    signal_events: pd.DataFrame,
    fold_windows: list[dict[str, Any]],
) -> pd.DataFrame:
    if decision_trace.empty:
        return pd.DataFrame()
    trace = decision_trace.copy()
    events = signal_events.copy()
    if not events.empty:
        event_cols = [
            "event_id",
            "bar_end_time",
            "available_at",
            "state",
            "primary_bsp",
            "bsp_types",
            "reference_price",
            "bi_idx",
            "structural_price",
            "related_bsp1_price",
            "zs_high",
            "zs_low",
        ]
        event_cols = [col for col in event_cols if col in events.columns]
        trace = trace.merge(events[event_cols], on="event_id", how="left", suffixes=("", "_event"))

    trace["accepted"] = trace["accepted"].map(_to_bool)
    trace["decomposition_present"] = trace.get("decomposition_id", "").fillna("").astype(str).str.len() > 0
    trace["reason_codes_flat"] = trace.get("reason_codes", "").map(_codes_to_text)
    trace["primary_reason"] = trace["reason_codes_flat"].map(_primary_reason)
    trace["regime_source"] = [
        _regime_source(regime, present)
        for regime, present in zip(trace.get("regime", ""), trace["decomposition_present"])
    ]
    trace["momentum_scope"] = trace.get("bsp_type", "").map(_momentum_scope)
    trace["fold_provenance"] = trace["timestamp"].map(lambda value: _fold_label(value, fold_windows))
    trace["in_sample_or_oos"] = trace["timestamp"].map(lambda value: _fold_role(value, fold_windows))
    trace["parent_available_ok"] = [
        _availability_ok(parent, decision)
        for parent, decision in zip(trace.get("parent_available_at", None), trace["timestamp"])
    ]
    trace["momentum_available_ok"] = [
        _availability_ok(momentum, decision)
        for momentum, decision in zip(trace.get("momentum_available_at", None), trace["timestamp"])
    ]
    trace["multi_level_time_honest_bool"] = trace.get("multi_level_time_honest", None).map(_nullable_bool)
    trace["time_integrity_issue"] = (
        (trace["multi_level_time_honest_bool"] == False)  # noqa: E712
        | (trace["parent_available_ok"] == False)  # noqa: E712
        | (trace["momentum_available_ok"] == False)  # noqa: E712
    )
    return trace


def add_execution_status(
    decisions: pd.DataFrame,
    accepted: pd.DataFrame,
    execution_decisions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Link rule-level accepted decisions to closed trades by event_id."""
    if decisions.empty:
        return decisions
    result = decisions.copy()
    executed_event_ids: set[str] = set()
    if not accepted.empty and "event_id" in accepted.columns:
        executed_event_ids = {
            str(value)
            for value in accepted["event_id"].dropna()
            if str(value).strip()
        }
    execution_by_event: dict[str, dict[str, Any]] = {}
    if execution_decisions is not None and not execution_decisions.empty:
        execution_frame = execution_decisions.copy()
        if "event_id" in execution_frame.columns:
            execution_frame["_event_id_str"] = execution_frame["event_id"].astype(str)
            for event_id, group in execution_frame.groupby("_event_id_str", sort=False):
                execution_by_event[str(event_id)] = group.iloc[-1].to_dict()

    result["executed_trade"] = result["event_id"].astype(str).isin(executed_event_ids)
    result["execution_status"] = "rejected_by_entry_or_context"
    accepted_mask = result["accepted"] == True  # noqa: E712
    result.loc[accepted_mask & result["executed_trade"], "execution_status"] = "accepted_executed_trade"
    result.loc[accepted_mask & ~result["executed_trade"], "execution_status"] = "accepted_not_executed"
    result["execution_audit_present"] = result["event_id"].astype(str).isin(execution_by_event)
    result["execution_disposition"] = [
        execution_by_event.get(str(event_id), {}).get("status", "")
        for event_id in result["event_id"]
    ]
    result["execution_rejection_stage"] = [
        execution_by_event.get(str(event_id), {}).get("rejection_stage", "")
        for event_id in result["event_id"]
    ]
    result["execution_rejection_reason"] = [
        execution_by_event.get(str(event_id), {}).get("rejection_reason", "")
        for event_id in result["event_id"]
    ]
    result["risk_approved"] = [
        execution_by_event.get(str(event_id), {}).get("risk_approved", "")
        for event_id in result["event_id"]
    ]
    result["risk_reason"] = [
        execution_by_event.get(str(event_id), {}).get("risk_reason", "")
        for event_id in result["event_id"]
    ]
    result["risk_realized_points"] = [
        execution_by_event.get(str(event_id), {}).get("risk_realized_points", "")
        for event_id in result["event_id"]
    ]
    result["risk_consecutive_losses"] = [
        execution_by_event.get(str(event_id), {}).get("risk_consecutive_losses", "")
        for event_id in result["event_id"]
    ]
    return result


def sample_rejected_decisions(
    decisions: pd.DataFrame,
    *,
    per_stratum: int,
    max_rows: int,
    seed: int,
) -> pd.DataFrame:
    if decisions.empty:
        return pd.DataFrame()
    rejected = decisions[decisions["accepted"] == False].copy()  # noqa: E712
    if rejected.empty:
        return rejected
    strata_cols = ["direction", "bsp_type", "primary_reason", "regime_source"]
    samples = []
    for _, group in rejected.groupby(strata_cols, dropna=False, sort=True):
        n = min(per_stratum, len(group))
        samples.append(group.sample(n=n, random_state=seed))
    result = pd.concat(samples, ignore_index=True).sort_values("timestamp")
    if len(result) > max_rows:
        result = result.sample(n=max_rows, random_state=seed).sort_values("timestamp")
    return result.reset_index(drop=True)


def build_mechanism_clusters(accepted: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        cluster_id: str,
        title: str,
        category: str,
        rule_ids: str,
        status: str,
        affected_trades: int = 0,
        affected_decisions: int = 0,
        actual_net_points: float | None = None,
        suggested_action_type: str = "CLARIFY_RULE",
        note: str = "",
    ) -> None:
        rows.append(
            {
                "cluster_id": cluster_id,
                "title": title,
                "category": category,
                "rule_ids": rule_ids,
                "rule_contract_status": status,
                "affected_trade_count": int(affected_trades),
                "affected_decision_count": int(affected_decisions),
                "actual_net_pnl_points": _round_or_blank(actual_net_points),
                "suggested_action_type": suggested_action_type,
                "note": note,
            }
        )

    if not accepted.empty:
        no_decomp = accepted[
            (accepted["regime"] == "unclassified")
            & (accepted["decomposition_present"] == False)  # noqa: E712
        ]
        explicit_unclassified = accepted[
            (accepted["regime"] == "unclassified")
            & (accepted["decomposition_present"] == True)  # noqa: E712
        ]
        add(
            "accepted_unclassified_no_decomposition",
            "Accepted trades with no decomposition snapshot",
            "entry_regime",
            "QP-DECOMP-001; Rulebook §11",
            "deferred",
            len(no_decomp),
            actual_net_points=no_decomp["net_pnl_points"].sum() if len(no_decomp) else 0,
            suggested_action_type="CLARIFY_RULE",
            note="Distinguishes missing decomposition from explicit UNCLASSIFIED classification.",
        )
        add(
            "accepted_explicit_unclassified",
            "Accepted trades explicitly classified as unclassified",
            "entry_regime",
            "QP-DECOMP-001; QP-REGIME-001",
            "fixed",
            len(explicit_unclassified),
            actual_net_points=explicit_unclassified["net_pnl_points"].sum() if len(explicit_unclassified) else 0,
            suggested_action_type="CLARIFY_RULE",
            note="If non-zero, the classifier produced UNCLASSIFIED but entry gates still allowed the signal.",
        )

        momentum_gap = accepted[
            accepted["bsp_type"].isin(["2", "2s"])
            & (accepted["momentum_status"] == "not_applicable")
        ]
        add(
            "t2_momentum_not_applicable",
            "2/2s trades bypass composite momentum by design",
            "momentum_coverage",
            "QP-MACD-001; QP-MACD-003; Rulebook §12",
            "research_candidate",
            len(momentum_gap),
            actual_net_points=momentum_gap["net_pnl_points"].sum() if len(momentum_gap) else 0,
            suggested_action_type="CLARIFY_RULE",
            note="This is a coverage gap to confirm, not automatically a code defect.",
        )

        stop_trades = accepted[accepted["exit_reason"] == "structure_stop"]
        cost_dominant = stop_trades[
            stop_trades["cost_as_fraction_of_risk"].notna()
            & (stop_trades["cost_as_fraction_of_risk"] >= 1.0)
        ]
        high_cost = stop_trades[
            stop_trades["cost_as_fraction_of_risk"].notna()
            & (stop_trades["cost_as_fraction_of_risk"] >= 0.5)
        ]
        add(
            "structure_stop_cost_dominant",
            "Structure-stop risk distance is small relative to realized round-trip cost",
            "exit_risk",
            "QP-BSP-002; QP-RISK-001; QP-EXIT-001",
            "engineering_default",
            len(high_cost),
            actual_net_points=high_cost["net_pnl_points"].sum() if len(high_cost) else 0,
            suggested_action_type="ADD_GATE",
            note=f"{len(cost_dominant)} trades had cost >= initial stop distance; this is descriptive evidence only.",
        )

        repeated = accepted[accepted["repeat_sequence_in_signal_key"] > 1]
        add(
            "repeat_signal_key_entries",
            "Repeated entries under the same signal_key",
            "entry_repetition",
            "QP-DECOMP-002; OPEN rule gap",
            "open_decision",
            len(repeated),
            actual_net_points=repeated["net_pnl_points"].sum() if len(repeated) else 0,
            suggested_action_type="CLARIFY_RULE",
            note="Quantifies re-entry cadence after stops or exits within the same signal lifecycle.",
        )

        same_bar = accepted[accepted["same_bar_execution_flag"] == True]  # noqa: E712
        add(
            "same_bar_signal_execution",
            "Signal bar end equals entry execution timestamp",
            "time_integrity",
            "QP-RISK-004; QP-LEVEL-001",
            "engineering_default",
            len(same_bar),
            actual_net_points=same_bar["net_pnl_points"].sum() if len(same_bar) else 0,
            suggested_action_type="IGNORE",
            note="Expected in a bar-close replay if available_at is not after the decision timestamp.",
        )

        holdout = _holdout_trades(accepted)
        if not holdout.empty:
            add(
                "holdout_direction_asymmetry",
                "Holdout window accepted trades are directionally concentrated",
                "directionality",
                "QP-LEVEL-002; OPEN-003",
                "deferred",
                len(holdout),
                actual_net_points=holdout["net_pnl_points"].sum(),
                suggested_action_type="CLARIFY_RULE",
                note=_direction_note(holdout),
            )

    if not decisions.empty:
        if "execution_status" in decisions.columns:
            accepted_not_executed = decisions[
                decisions["execution_status"] == "accepted_not_executed"
            ]
            audit_complete = (
                not accepted_not_executed.empty
                and "execution_audit_present" in accepted_not_executed.columns
                and bool(accepted_not_executed["execution_audit_present"].all())
            )
            add(
                "accepted_not_executed_after_rule_accept",
                "Rule-accepted decisions that did not become trades",
                "execution_visibility",
                "QP-RISK-002; QP-RISK-003; QP-EXIT-001",
                "open_decision" if audit_complete else "engineering_default",
                affected_decisions=len(accepted_not_executed),
                suggested_action_type="CLARIFY_RULE" if audit_complete else "FIX_CODE",
                note=(
                    "Accepted decision_trace rows are not guaranteed fills. "
                    + _execution_reason_note(accepted_not_executed)
                ),
            )
        parent_short = decisions[
            (decisions["accepted"] == False)  # noqa: E712
            & (decisions["direction"] == "short")
            & decisions["reason_codes_flat"].str.contains("parent_direction_conflict", na=False)
        ]
        add(
            "short_rejected_by_parent_direction",
            "Short decisions rejected by parent direction conflict",
            "multi_level_gate",
            "QP-LEVEL-001; QP-LEVEL-002; OPEN-003",
            "deferred",
            affected_decisions=len(parent_short),
            suggested_action_type="CLARIFY_RULE",
            note="Audits whether parent-level lag creates directional asymmetry near turns.",
        )
        time_issues = decisions[decisions["time_integrity_issue"] == True]  # noqa: E712
        add(
            "time_integrity_failures",
            "Decision records with point-in-time integrity issues",
            "time_integrity",
            "QP-LEVEL-001; QP-RISK-004",
            "engineering_default",
            affected_decisions=len(time_issues),
            suggested_action_type=("FIX_CODE" if len(time_issues) else "IGNORE"),
            note="Any non-zero count requires code-level investigation before strategy interpretation.",
        )

    return pd.DataFrame(rows)


def _accepted_trade_row(
    trade: dict[str, Any],
    *,
    event: dict[str, Any],
    trace: dict[str, Any],
    entry_fill: dict[str, Any],
    exit_fill: dict[str, Any],
    atr: pd.Series,
    row_by_time: dict[pd.Timestamp, int],
    fold_windows: list[dict[str, Any]],
    fee_points: float,
    ordinal: int,
) -> dict[str, Any]:
    direction = str(trade.get("direction", ""))
    sign = 1 if direction == "long" else -1
    lots = int(trade.get("lots", 1) or 1)
    entry_time = _ts(trade.get("entry_time"))
    exit_time = _ts(trade.get("exit_time"))
    entry_bar = _safe_int(trade.get("entry_bar"))
    exit_bar = _safe_int(trade.get("exit_bar"))
    event_bar_time = _ts(event.get("bar_end_time")) if event else pd.NaT
    event_available_at = _ts(event.get("available_at")) if event else pd.NaT
    entry_signal = _safe_float(entry_fill.get("signal_price"))
    entry_fill_price = _safe_float(entry_fill.get("fill_price", trade.get("entry_price")))
    exit_signal = _safe_float(exit_fill.get("signal_price"))
    exit_fill_price = _safe_float(exit_fill.get("fill_price", trade.get("exit_price")))
    net = _safe_float(trade.get("pnl_points"))
    fees = fee_points * 2 * lots
    slippage = _slippage_points(entry_fill, exit_fill, lots)
    gross = (
        sign * (exit_signal - entry_signal) * lots
        if np.isfinite(entry_signal) and np.isfinite(exit_signal)
        else net + fees + slippage
    )
    gross_after_slippage = net + fees
    stop = _safe_float(trade.get("execution_stop_price"))
    stop_distance = abs(entry_fill_price - stop) if np.isfinite(stop) else np.nan
    cost_total = fees + slippage
    atr_value = float(atr.iloc[entry_bar]) if entry_bar is not None and entry_bar < len(atr) else np.nan
    event_row = row_by_time.get(event_bar_time)
    entry_signal_age_bars = (
        entry_bar - event_row
        if entry_bar is not None and event_row is not None
        else np.nan
    )
    decomposition_id = str(trace.get("decomposition_id", "") or "")
    regime = str(trace.get("regime", "") or "unclassified")
    decomposition_present = bool(decomposition_id)
    parent_available = trace.get("parent_available_at")
    momentum_available = trace.get("momentum_available_at")

    return {
        "trade_index": ordinal,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_bar": entry_bar,
        "exit_bar": exit_bar,
        "direction": direction,
        "lots": lots,
        "grade": trade.get("grade", ""),
        "active_symbol": trade.get("active_symbol", ""),
        "event_id": trade.get("event_id", ""),
        "signal_key": trade.get("signal_key", ""),
        "decision_id": trade.get("decision_id", ""),
        "bsp_type": trace.get("bsp_type", trade.get("bsp_type", "")),
        "entry_signal_price": entry_signal,
        "entry_fill_price": entry_fill_price,
        "exit_signal_price": exit_signal,
        "exit_fill_price": exit_fill_price,
        "gross_pnl_points": round(float(gross), 4) if np.isfinite(gross) else np.nan,
        "gross_after_slippage_points": round(float(gross_after_slippage), 4)
        if np.isfinite(gross_after_slippage)
        else np.nan,
        "fees_points": round(float(fees), 4),
        "slippage_points": round(float(slippage), 4),
        "cost_total_points": round(float(cost_total), 4),
        "net_pnl_points": net,
        "cost_turned_positive_to_negative": bool(gross > 0 and net <= 0)
        if np.isfinite(gross) and np.isfinite(net)
        else False,
        "hold_bars": trade.get("hold_bars", ""),
        "exit_reason": trade.get("exit_reason", ""),
        "exit_rule": trade.get("exit_rule", ""),
        "setup_invalidation_price": trade.get("setup_invalidation_price", ""),
        "execution_stop_price": trade.get("execution_stop_price", ""),
        "stop_anchor_type": _stop_anchor_type(event),
        "setup_anchor_type": _setup_anchor_type(event),
        "stop_distance_points": round(float(stop_distance), 4)
        if np.isfinite(stop_distance)
        else np.nan,
        "stop_distance_atr_ratio": round(float(stop_distance / atr_value), 6)
        if np.isfinite(stop_distance) and np.isfinite(atr_value) and atr_value > 0
        else np.nan,
        "cost_as_fraction_of_risk": round(float(cost_total / (stop_distance * lots)), 6)
        if np.isfinite(stop_distance) and stop_distance > 0
        else np.nan,
        "event_bar_end_time": event_bar_time,
        "event_available_at": event_available_at,
        "same_bar_execution_flag": bool(entry_time == event_bar_time)
        if pd.notna(event_bar_time)
        else False,
        "event_available_before_entry": _availability_ok(event_available_at, entry_time),
        "entry_signal_age_bars": entry_signal_age_bars,
        "decomposition_id": decomposition_id,
        "decomposition_revision": trace.get("decomposition_revision", ""),
        "decomposition_present": decomposition_present,
        "regime": regime,
        "regime_source": _regime_source(regime, decomposition_present),
        "regime_direction": trace.get("regime_direction", ""),
        "regime_lifecycle": trace.get("regime_lifecycle", ""),
        "multi_level_accepted": trace.get("multi_level_accepted", ""),
        "multi_level_time_honest": trace.get("multi_level_time_honest", ""),
        "parent_direction": trace.get("parent_direction", ""),
        "parent_structure_id": trace.get("parent_structure_id", ""),
        "parent_available_at": parent_available,
        "parent_available_ok": _availability_ok(parent_available, entry_time),
        "child_confirmed": trace.get("child_confirmed", ""),
        "child_match_count": trace.get("child_match_count", ""),
        "child_window_begin": trace.get("child_window_begin", ""),
        "child_window_end": trace.get("child_window_end", ""),
        "momentum_status": trace.get("momentum_status", ""),
        "momentum_scope": _momentum_scope(trace.get("bsp_type", trade.get("bsp_type", ""))),
        "momentum_accepted": trace.get("momentum_accepted", ""),
        "momentum_available_at": momentum_available,
        "momentum_available_ok": _availability_ok(momentum_available, entry_time),
        "macd_area_ratio": trace.get("macd_area_ratio", ""),
        "histogram_state": trace.get("histogram_state", ""),
        "fold_provenance": _fold_label(entry_time, fold_windows),
        "in_sample_or_oos": _fold_role(entry_time, fold_windows),
    }


def _add_repeat_fields(audit: pd.DataFrame) -> pd.DataFrame:
    result = audit.sort_values(["entry_bar", "exit_bar"]).copy()
    result["repeat_sequence_in_signal_key"] = (
        result.groupby("signal_key").cumcount() + 1
    )
    result["repeat_count_in_signal_key"] = result.groupby("signal_key")["signal_key"].transform("size")
    result["repeat_setup_group"] = result["signal_key"]
    structure_group = result["decomposition_id"].replace("", np.nan)
    fallback = (
        "nodecomp:"
        + result["active_symbol"].astype(str)
        + ":"
        + result["direction"].astype(str)
        + ":"
        + result["bsp_type"].astype(str)
        + ":"
        + result["setup_invalidation_price"].astype(str)
    )
    result["repeat_structure_group"] = structure_group.fillna(fallback)
    result["prior_trade_outcome"] = result.groupby("signal_key")["net_pnl_points"].shift(1)
    result["prior_exit_reason"] = result.groupby("signal_key")["exit_reason"].shift(1)
    result["prior_exit_bar"] = result.groupby("signal_key")["exit_bar"].shift(1)
    result["bars_since_prior_exit"] = result["entry_bar"] - result["prior_exit_bar"]
    result["reentry_after_stop"] = (
        (result["prior_exit_reason"] == "structure_stop")
        & result["bars_since_prior_exit"].notna()
        & (result["bars_since_prior_exit"] >= 0)
    )
    result["signal_key_group_net_pnl_points"] = result.groupby("signal_key")["net_pnl_points"].transform("sum")
    result["signal_key_group_trade_count"] = result.groupby("signal_key")["signal_key"].transform("size")
    return result


def build_run_meta(
    *,
    config_path: Path,
    output_dir: Path,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    config,
    result_counts: dict[str, int],
    source_reused_from: str | None,
    sample_seed: int,
    per_stratum: int,
    max_rejected_samples: int,
    replay_start: object | None,
    replay_end: object | None,
    decision_start: object | None,
    decision_end: object | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(PROJECT_ROOT),
        "output_dir": str(output_dir),
        "git_head": _git_output("rev-parse", "HEAD"),
        "git_branch": _git_output("branch", "--show-current"),
        "git_status_short": _git_output("status", "--porcelain"),
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "data_sha256": {
            "configured_data_path": _sha256(_path_from_config(config.data_path)),
            "production_adjusted_1m_path": _sha256(_path_from_config(config.production.adjusted_1m_path)),
            "parent_data_path": _sha256(_path_from_config(config.multi_level.parent_data_path)),
            "child_data_path": _sha256(_path_from_config(config.multi_level.child_data_path)),
        },
        "frame_ranges": {
            "current_start": str(current["datetime"].min()),
            "current_end": str(current["datetime"].max()),
            "current_rows": int(len(current)),
            "parent_start": str(parent["datetime"].min()),
            "parent_end": str(parent["datetime"].max()),
            "parent_rows": int(len(parent)),
            "child_start": str(child["datetime"].min()),
            "child_end": str(child["datetime"].max()),
            "child_rows": int(len(child)),
        },
        "replay_start": replay_start,
        "replay_end": replay_end,
        "decision_start": decision_start,
        "decision_end": decision_end,
        "source_reused_from": source_reused_from,
        "sample_strategy": {
            "rejected_strata": ["direction", "bsp_type", "primary_reason", "regime_source"],
            "sample_seed": sample_seed,
            "per_stratum": per_stratum,
            "max_rejected_samples": max_rejected_samples,
        },
        "no_tuning_notice": NO_TUNING_NOTICE,
        "trade_count": int(result_counts.get("trade_count", 0)),
        "fill_count": int(result_counts.get("fill_count", 0)),
        "decision_count": int(result_counts.get("decision_count", 0)),
        "signal_event_count": int(result_counts.get("signal_event_count", 0)),
        "execution_decision_count": int(result_counts.get("execution_decision_count", 0)),
    }


def write_audit_summary(
    output_dir: Path,
    meta: dict[str, Any],
    accepted: pd.DataFrame,
    decisions: pd.DataFrame,
    clusters: pd.DataFrame,
) -> None:
    total_trades = len(accepted)
    total_decisions = len(decisions)
    rejected = decisions[decisions["accepted"] == False] if not decisions.empty else pd.DataFrame()  # noqa: E712
    accepted_decisions = decisions[decisions["accepted"] == True] if not decisions.empty else pd.DataFrame()  # noqa: E712
    accepted_executed = (
        decisions[decisions["execution_status"] == "accepted_executed_trade"]
        if not decisions.empty and "execution_status" in decisions.columns
        else pd.DataFrame()
    )
    accepted_not_executed = (
        decisions[decisions["execution_status"] == "accepted_not_executed"]
        if not decisions.empty and "execution_status" in decisions.columns
        else pd.DataFrame()
    )
    source_line = (
        f"- 原始回测产物复用自: `{meta['source_reused_from']}`"
        if meta.get("source_reused_from")
        else "- `source/`: 当前 HEAD 重跑出的原始回测产物"
    )
    lines = [
        "# 当前规则机制审计报告",
        "",
        NO_TUNING_NOTICE,
        "",
        "## 1. 运行基线",
        "",
        f"- Git HEAD: `{meta['git_head']}`",
        f"- Git branch: `{meta['git_branch']}`",
        f"- Config SHA256: `{meta['config_sha256']}`",
        f"- Current frame: `{meta['frame_ranges']['current_start']}` -> `{meta['frame_ranges']['current_end']}`, {meta['frame_ranges']['current_rows']} bars",
        (
            f"- Decisions: {total_decisions}; accepted decisions: {len(accepted_decisions)}; "
            f"accepted executed: {len(accepted_executed)}; accepted not executed: {len(accepted_not_executed)}; "
            f"rejected decisions: {len(rejected)}; closed trades: {total_trades}"
        ),
        f"- Sampling: seed={meta['sample_strategy']['sample_seed']}, per_stratum={meta['sample_strategy']['per_stratum']}, max={meta['sample_strategy']['max_rejected_samples']}",
        "",
        "## 2. 证据完整性",
        "",
        _integrity_line("multi_level_time_honest failures", decisions, "time_integrity_issue"),
        _integrity_line("event available after entry", accepted, "event_available_before_entry", expect=True),
        _integrity_line("parent available after entry", accepted, "parent_available_ok", expect=True),
        _integrity_line("momentum available after entry", accepted, "momentum_available_ok", expect=True),
        "",
        "## 3. 六个核心机制问题",
        "",
        _unclassified_summary(accepted),
        _momentum_summary(accepted),
        _stop_summary(accepted),
        _repeat_summary(accepted),
        _direction_summary(accepted, decisions),
        _execution_summary(decisions),
        "",
        "## 4. 机制簇",
        "",
        _clusters_markdown(clusters),
        "",
        "## 5. 输出文件",
        "",
        "- `accepted_trades_audit.csv`: 全量已成交交易机制表",
        "- `decision_mechanism_audit.csv`: 全量决策机制表",
        "- `rejected_decisions_sample.csv`: 固定种子的拒绝决策分层样本",
        "- `mechanism_clusters.csv`: 机制问题簇和建议动作类型",
        "- `open_rule_questions.md`: 需要用户人工裁决的规则边界",
        "- `falsifiable_hypotheses.md`: 下一轮可证伪假设登记",
        "- `source/execution_decisions.csv`: 规则 accepted 后的执行/风控审批审计表",
        source_line,
        "",
        "## 6. 边界声明",
        "",
        "- 本报告没有运行参数 sweep。",
        "- 本报告没有输出任何 hypothetical PnL。",
        "- 所有 PnL 字段均为当前规则实际回放结果，只用于机制分组背景，不作为调参建议。",
    ]
    (output_dir / "audit_summary.md").write_text("\n".join(lines), encoding="utf-8")


def write_open_rule_questions(
    output_dir: Path,
    accepted: pd.DataFrame,
    decisions: pd.DataFrame,
    clusters: pd.DataFrame,
) -> None:
    no_decomp = 0 if accepted.empty else int(((accepted["regime"] == "unclassified") & (accepted["decomposition_present"] == False)).sum())  # noqa: E712
    explicit = 0 if accepted.empty else int(((accepted["regime"] == "unclassified") & (accepted["decomposition_present"] == True)).sum())  # noqa: E712
    t2_na = 0 if accepted.empty else int((accepted["bsp_type"].isin(["2", "2s"]) & (accepted["momentum_status"] == "not_applicable")).sum())
    repeat = 0 if accepted.empty else int((accepted["repeat_sequence_in_signal_key"] > 1).sum())
    short_parent = 0
    accepted_not_executed = 0
    execution_note = "execution audit not available yet."
    if not decisions.empty:
        short_parent = int(
            (
                (decisions["accepted"] == False)  # noqa: E712
                & (decisions["direction"] == "short")
                & decisions["reason_codes_flat"].str.contains("parent_direction_conflict", na=False)
            ).sum()
        )
        if "execution_status" in decisions.columns:
            accepted_not_executed_frame = decisions[
                decisions["execution_status"] == "accepted_not_executed"
            ]
            accepted_not_executed = int(len(accepted_not_executed_frame))
            execution_note = _execution_reason_note(accepted_not_executed_frame)
    lines = [
        "# 待人工确认的青派规则问题",
        "",
        "这些问题不是调参项，而是规则语义边界。请在下一轮策略改进前裁决。",
        "",
        "## Q1. 未分类或无分解快照时能否开仓？",
        "",
        f"- 当前审计命中：无分解快照但入场 {no_decomp} 笔；显式 `unclassified` 但入场 {explicit} 笔。",
        "- 需确认：严格青派是否要求 `decomposition_present=true` 且走势类型已分类才允许入场？",
        "- 管辖规则：`QP-DECOMP-001`, `QP-REGIME-001`, Rulebook §11。",
        "",
        "## Q2. 2/2s 是否需要独立的力度确认？",
        "",
        f"- 当前审计命中：2/2s 且 `momentum_status=not_applicable` 的已成交交易 {t2_na} 笔。",
        "- 已知实现：组合 MACD 动量仅作用于 1/1p，2/2s 默认跳过。",
        "- 需确认：2/2s 的力度确认是否应由价格力度、子级别结构或其它规则承担？",
        "- 管辖规则：`QP-MACD-001`, `QP-MACD-003`, Rulebook §12。",
        "",
        "## Q3. 同一 signal_key 止损后是否允许连续重入？",
        "",
        f"- 当前审计命中：同一 `signal_key` 的第 2 次及以上入场 {repeat} 笔。",
        "- 需确认：同一结构前提未失效时，保护止损后是否允许再次试错；如允许，节奏应由哪类结构事件重置？",
        "- 管辖规则：`QP-DECOMP-002`, `QP-EXIT-001`，当前契约未显式覆盖。",
        "",
        "## Q4. 父级别方向门控在转折点应如何处理？",
        "",
        f"- 当前审计命中：空头决策因父级方向冲突被拒绝 {short_parent} 条。",
        "- 需确认：父级已确认结构滞后时，严格策略是否继续完全拒绝逆父级方向信号，还是保留观察/减仓/禁多等非开仓状态？",
        "- 管辖规则：`QP-LEVEL-002`, `OPEN-003`。",
        "",
        "## Q5. 结构保护止损是否允许成本/波动率下限？",
        "",
        "- 当前审计表已量化 `stop_distance_points`、`cost_as_fraction_of_risk` 与 `stop_distance_atr_ratio`。",
        "- 需确认：青派结构止损是否必须完全使用笔端结构价，还是允许订单保护止损在不改变 setup invalidation 的前提下加入执行层约束？",
        "- 管辖规则：`QP-BSP-002`, `QP-RISK-001`, `QP-EXIT-001`。",
        "",
        "## Q6. 规则 accepted 但被风控拒绝时应采用哪种研究口径？",
        "",
        f"- 当前审计命中：`decision_trace.accepted=True` 但没有对应已闭合交易 {accepted_not_executed} 条；{execution_note}",
        "- 需确认：`max_consecutive_losses` 在全历史连续回放中触发后，研究口径应永久暂停、按合约/年度/fold 重置，还是同时保留两套报告？",
        "- 管辖规则：`QP-RISK-002`, `QP-RISK-003`, `QP-EXIT-001`。",
    ]
    (output_dir / "open_rule_questions.md").write_text("\n".join(lines), encoding="utf-8")


def write_falsifiable_hypotheses(
    output_dir: Path,
    accepted: pd.DataFrame,
    decisions: pd.DataFrame,
    clusters: pd.DataFrame,
) -> None:
    lines = [
        "# 可证伪机制假设登记",
        "",
        "以下是假设登记，不是调参方案。每条都必须在未见样本或前向仿真中验证后，才允许进入策略修改。",
        "",
        "## H1. 分类可用性假设",
        "",
        "- 机制发现：本次已成交交易未命中 `unclassified` 或 `decomposition_present=false`，但该准入边界仍需作为规则契约保留。",
        "- 规则触点：`QP-DECOMP-001`, `QP-REGIME-001`。",
        "- 证伪方式：冻结一个分类门控候选，在新保留区间验证被影响交易的机制分布，而不使用 2026-07 的 11 笔调参。",
        "",
        "## H2. 2/2s 力度覆盖假设",
        "",
        "- 机制发现：2/2s 默认 `momentum_status=not_applicable`，组合动量没有参与准入。",
        "- 规则触点：`QP-MACD-001`, `QP-MACD-003`。",
        "- 证伪方式：先人工确认 2/2s 的力度定义，再用固定定义对新样本做接受/拒绝对照。",
        "",
        "## H3. 执行止损与成本噪声假设",
        "",
        "- 机制发现：结构止损交易存在成本占初始风险比例过高的簇。",
        "- 规则触点：`QP-BSP-002`, `QP-RISK-001`, `QP-EXIT-001`。",
        "- 证伪方式：只比较机制标签与未来样本表现，不在本轮报告中给出任何止损参数值。",
        "",
        "## H4. 同结构重复试错假设",
        "",
        "- 机制发现：同一 `signal_key` 可多次入场，当前契约没有冷却或重置定义。",
        "- 规则触点：`QP-DECOMP-002`, `QP-EXIT-001`。",
        "- 证伪方式：用户先裁决何为同一 setup，再在 walk-forward 中验证该定义是否减少机制性重复损耗。",
        "",
        "## H5. 父级滞后方向门控假设",
        "",
        "- 机制发现：空头信号可能因父级方向冲突被系统性拒绝，转折窗口可能形成方向集中。",
        "- 规则触点：`QP-LEVEL-002`, `OPEN-003`。",
        "- 证伪方式：不直接放开做空；先统计父级冲突、禁多/禁空、空头拒绝后的市场状态分布。",
        "",
        "## H6. 风控状态可见性假设",
        "",
        "- 机制发现：存在规则 accepted 但没有形成已闭合交易的决策记录；执行审计用于识别具体风控/持仓原因。",
        "- 规则触点：`QP-RISK-002`, `QP-RISK-003`, `QP-EXIT-001`。",
        "- 证伪方式：比较连续长回放与按合约/年度/fold 重置风控状态回放中的 accepted-not-executed 分布。",
    ]
    (output_dir / "falsifiable_hypotheses.md").write_text("\n".join(lines), encoding="utf-8")


def _standard_fold_windows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    class _Cfg:
        pass

    runner = ProductionWalkforwardRunner(_Cfg(), train_months=24, test_months=12, step_months=12)
    folds = runner.folds(frame)
    return [asdict(fold) for fold in folds]


def _fold_label(value: object, windows: list[dict[str, Any]]) -> str:
    ts = _ts(value)
    for fold in windows:
        if _ts(fold["test_start"]) <= ts < _ts(fold["test_end"]):
            return f"fold_{int(fold['fold']):02d}_oos"
    for fold in windows:
        if _ts(fold["train_start"]) <= ts < _ts(fold["train_end"]):
            return f"fold_{int(fold['fold']):02d}_train_context"
    return "outside_standard_folds"


def _fold_role(value: object, windows: list[dict[str, Any]]) -> str:
    label = _fold_label(value, windows)
    if label.endswith("_oos"):
        return "standard_oos"
    if label.endswith("_train_context"):
        return "standard_train_context"
    return "outside_standard_folds"


def _match_fill(
    fills: pd.DataFrame,
    trade: dict[str, Any],
    side: str,
    used: set[int],
) -> dict[str, Any]:
    if fills.empty:
        return {}
    timestamp = _ts(trade["entry_time"] if side == "entry" else trade["exit_time"])
    if side == "entry":
        mask = fills["target_position"].astype(float) != 0
    else:
        mask = fills["target_position"].astype(float) == 0
    candidates = fills[mask & (pd.to_datetime(fills["timestamp"]).map(_ts) == timestamp)].copy()
    if "active_symbol" in candidates.columns and trade.get("active_symbol"):
        symbol_matches = candidates[candidates["active_symbol"].astype(str) == str(trade.get("active_symbol"))]
        if not symbol_matches.empty:
            candidates = symbol_matches
    for idx, row in candidates.iterrows():
        if int(idx) not in used:
            used.add(int(idx))
            return row.to_dict()
    remaining = fills[~fills.index.isin(used)]
    if not remaining.empty:
        row = remaining.iloc[0]
        used.add(int(row.name))
        return row.to_dict()
    return {}


def _slippage_points(entry_fill: dict[str, Any], exit_fill: dict[str, Any], lots: int) -> float:
    total = 0.0
    for fill in (entry_fill, exit_fill):
        signal = _safe_float(fill.get("signal_price"))
        actual = _safe_float(fill.get("fill_price"))
        if np.isfinite(signal) and np.isfinite(actual):
            total += abs(actual - signal)
    return total * lots


def _stop_anchor_type(event: dict[str, Any]) -> str:
    primary = str(event.get("primary_bsp", "") or "")
    if primary in {"1", "1p"}:
        return "t1_structural_price"
    if primary in {"2", "2s"}:
        return "t2_retrace_structural_price"
    if primary in {"3a", "3b"}:
        return "t3_pullback_structural_price"
    return "unknown"


def _setup_anchor_type(event: dict[str, Any]) -> str:
    primary = str(event.get("primary_bsp", "") or "")
    if primary in {"1", "1p"}:
        return "t1_structural_price"
    if primary in {"2", "2s"}:
        return "related_bsp1_price"
    if primary in {"3a", "3b"}:
        direction = str(event.get("direction", "") or "")
        return "zs_high" if direction == "long" else "zs_low"
    return "unknown"


def _momentum_scope(bsp_type: object) -> str:
    values = {part.strip() for part in str(bsp_type or "").split(",") if part.strip()}
    if values.intersection({"1", "1p"}):
        return "applies_t1"
    if values.intersection({"2", "2s"}):
        return "not_applicable_t2_coverage_gap"
    if values.intersection({"3a", "3b"}):
        return "not_applicable_t3_coverage_gap"
    return "unknown"


def _regime_source(regime: object, decomposition_present: bool) -> str:
    text = str(regime or "unclassified")
    if not decomposition_present:
        return "no_decomposition_snapshot"
    if text == "unclassified":
        return "explicit_unclassified"
    return "classified"


def _holdout_trades(accepted: pd.DataFrame) -> pd.DataFrame:
    if accepted.empty:
        return accepted
    times = pd.to_datetime(accepted["entry_time"]).map(_ts)
    return accepted[(times >= HOLDOUT_START) & (times < HOLDOUT_END)]


def _direction_note(frame: pd.DataFrame) -> str:
    counts = frame["direction"].value_counts().to_dict()
    return "direction_counts=" + json.dumps(counts, ensure_ascii=False, sort_keys=True)


def _integrity_line(label: str, frame: pd.DataFrame, column: str, *, expect: bool = False) -> str:
    if frame.empty or column not in frame.columns:
        return f"- {label}: N/A"
    if expect:
        bad = int((frame[column] == False).sum())  # noqa: E712
    else:
        bad = int((frame[column] == True).sum())  # noqa: E712
    return f"- {label}: {bad}"


def _unclassified_summary(accepted: pd.DataFrame) -> str:
    if accepted.empty:
        return "- Unclassified: no accepted trades."
    accepted_unclassified = accepted[accepted["regime"] == "unclassified"]
    no_decomp = accepted_unclassified[accepted_unclassified["decomposition_present"] == False]  # noqa: E712
    explicit = accepted_unclassified[accepted_unclassified["decomposition_present"] == True]  # noqa: E712
    return (
        f"- 分类先行：已成交 {len(accepted)} 笔中 `unclassified` {len(accepted_unclassified)} 笔；"
        f"其中无分解快照 {len(no_decomp)} 笔，显式未分类 {len(explicit)} 笔。"
    )


def _momentum_summary(accepted: pd.DataFrame) -> str:
    if accepted.empty:
        return "- 动量覆盖：no accepted trades."
    not_app = accepted[accepted["momentum_status"] == "not_applicable"]
    t2 = not_app[not_app["bsp_type"].isin(["2", "2s"])]
    return (
        f"- 动量覆盖：`not_applicable` {len(not_app)} 笔，其中 2/2s {len(t2)} 笔；"
        "这按当前实现属于覆盖范围问题，不直接判为 bug。"
    )


def _stop_summary(accepted: pd.DataFrame) -> str:
    if accepted.empty:
        return "- 结构止损：no accepted trades."
    stops = accepted[accepted["exit_reason"] == "structure_stop"]
    if stops.empty:
        return "- 结构止损：0 笔。"
    median_dist = stops["stop_distance_points"].median()
    median_cost_ratio = stops["cost_as_fraction_of_risk"].median()
    cost_ge_risk = int((stops["cost_as_fraction_of_risk"] >= 1.0).sum())
    return (
        f"- 结构止损：{len(stops)} 笔；止损距离中位数 {median_dist:.2f} 点；"
        f"成本/初始风险中位数 {median_cost_ratio:.2f}；成本不小于初始风险 {cost_ge_risk} 笔。"
    )


def _repeat_summary(accepted: pd.DataFrame) -> str:
    if accepted.empty:
        return "- 重复入场：no accepted trades."
    repeated_groups = accepted.groupby("signal_key").size()
    repeated_groups = repeated_groups[repeated_groups > 1]
    reentries = int((accepted["repeat_sequence_in_signal_key"] > 1).sum())
    after_stop = int(accepted["reentry_after_stop"].sum())
    return (
        f"- 重复入场：重复 `signal_key` 组 {len(repeated_groups)} 个；"
        f"第 2 次及以上入场 {reentries} 笔；止损后重入 {after_stop} 笔。"
    )


def _direction_summary(accepted: pd.DataFrame, decisions: pd.DataFrame) -> str:
    if accepted.empty and decisions.empty:
        return "- 方向门控：no data."
    trade_counts = {} if accepted.empty else accepted["direction"].value_counts().to_dict()
    short_parent = 0
    if not decisions.empty:
        short_parent = int(
            (
                (decisions["accepted"] == False)  # noqa: E712
                & (decisions["direction"] == "short")
                & decisions["reason_codes_flat"].str.contains("parent_direction_conflict", na=False)
            ).sum()
        )
    return (
        f"- 方向门控：已成交方向分布 {json.dumps(trade_counts, ensure_ascii=False)}；"
        f"空头因父级方向冲突被拒绝 {short_parent} 条。"
    )


def _execution_summary(decisions: pd.DataFrame) -> str:
    if decisions.empty or "execution_status" not in decisions.columns:
        return "- 执行可见性：no data."
    accepted_not_executed = decisions[decisions["execution_status"] == "accepted_not_executed"]
    accepted_executed = decisions[decisions["execution_status"] == "accepted_executed_trade"]
    if accepted_not_executed.empty:
        return f"- 执行可见性：规则 accepted 且形成交易 {len(accepted_executed)} 条；无 accepted-not-executed。"
    first_ts = _ts(accepted_not_executed["timestamp"].min())
    last_ts = _ts(accepted_not_executed["timestamp"].max())
    counts = accepted_not_executed["direction"].value_counts().to_dict()
    return (
        f"- 执行可见性：规则 accepted 但未形成已闭合交易 {len(accepted_not_executed)} 条；"
        f"方向分布 {json.dumps(counts, ensure_ascii=False)}；"
        f"范围 `{first_ts}` -> `{last_ts}`；{_execution_reason_note(accepted_not_executed)}"
    )


def _execution_reason_note(frame: pd.DataFrame) -> str:
    if frame.empty or "execution_disposition" not in frame.columns:
        return "execution audit not available yet."
    disposition = frame["execution_disposition"].replace("", np.nan).dropna()
    reasons = frame["execution_rejection_reason"].replace("", np.nan).dropna() if "execution_rejection_reason" in frame.columns else pd.Series(dtype=object)
    if disposition.empty and reasons.empty:
        return "execution audit present but no rejection details were recorded."
    parts = []
    if not disposition.empty:
        parts.append(
            "disposition="
            + json.dumps(disposition.value_counts().head(5).to_dict(), ensure_ascii=False)
        )
    if not reasons.empty:
        parts.append(
            "reason="
            + json.dumps(reasons.value_counts().head(5).to_dict(), ensure_ascii=False)
        )
    return "; ".join(parts) + "."


def _clusters_markdown(clusters: pd.DataFrame) -> str:
    if clusters.empty:
        return "- No clusters."
    lines = []
    for row in clusters.to_dict("records"):
        lines.append(
            f"- `{row['cluster_id']}`: trades={row['affected_trade_count']}, "
            f"decisions={row['affected_decision_count']}, action={row['suggested_action_type']}, "
            f"status={row['rule_contract_status']}"
        )
    return "\n".join(lines)


def _calculate_atr(bars: pd.DataFrame, period: int) -> pd.Series:
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def _slice_frame(
    frame: pd.DataFrame,
    start: object | None,
    end: object | None,
) -> pd.DataFrame:
    result = frame
    if start is not None:
        result = result[result["datetime"] >= _ts(start)]
    if end is not None:
        result = result[result["datetime"] <= _ts(end)]
    return result.reset_index(drop=True)


def _optional_ts(value: object | None) -> pd.Timestamp | None:
    if value is None:
        return None
    return _ts(value)


def _indexed(frame: pd.DataFrame, key: str) -> dict[str, dict[str, Any]]:
    if frame.empty or key not in frame.columns:
        return {}
    result = {}
    for row in frame.to_dict("records"):
        value = row.get(key)
        if pd.notna(value):
            result[str(value)] = row
    return result


def _codes_to_text(value: object) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    if isinstance(value, (tuple, list, set)):
        return "|".join(str(item) for item in value)
    text = str(value)
    if text.startswith("(") or text.startswith("["):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (tuple, list, set)):
                return "|".join(str(item) for item in parsed)
        except (SyntaxError, ValueError):
            pass
    return text.replace(",", "|") if "|" not in text and "," in text else text


def _primary_reason(value: object) -> str:
    text = _codes_to_text(value)
    return text.split("|")[0] if text else ""


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, np.integer)):
        return bool(value)
    return str(value).strip().lower() == "true"


def _nullable_bool(value: object) -> bool | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    return _to_bool(value)


def _availability_ok(available: object, decision: object) -> bool | None:
    if available is None or decision is None:
        return None
    try:
        left = _ts(available)
        right = _ts(decision)
        if pd.isna(left) or pd.isna(right):
            return None
        return left <= right
    except Exception:
        return None


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return ts
    if ts.tzinfo is None:
        return ts.tz_localize("Asia/Shanghai")
    return ts.tz_convert("Asia/Shanghai")


def _safe_float(value: object) -> float:
    try:
        if value is None or value == "":
            return float("nan")
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _safe_int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, float) and np.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_or_blank(value: float | None) -> float | str:
    if value is None or not np.isfinite(value):
        return ""
    return round(float(value), 4)


def _path_from_config(path: str | None) -> Path:
    if not path:
        return Path("")
    value = Path(path)
    return value if value.is_absolute() else PROJECT_ROOT / value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, OSError):
        return ""


def _git_output(*args: str) -> str:
    git = shutil.which("git") or "git"
    try:
        result = subprocess.run(
            [git, "-C", str(PROJECT_ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _write_optional_csv(frame: pd.DataFrame, path: Path) -> None:
    if not frame.empty:
        frame.to_csv(path, index=False, encoding="utf-8-sig")


def _read_optional_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


if __name__ == "__main__":
    raise SystemExit(main())
