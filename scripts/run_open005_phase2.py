"""Run OPEN-005 risk profiles, fold-reset and identity diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chan_futures.backtest import run_backtest
from chan_futures.config_loader import load_config
from chan_futures.feed import prepare_ohlc_frame
from chan_futures.production import (
    ProductionWalkforwardRunner,
    _replay_start_index,
    _slice,
    load_rb_production_frames,
)
from chan_futures.risk_policy import RiskGateMode, RiskProfile


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "rb_15m_qingpai_strict.yaml"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "open005_phase2"
PROFILE_ALIASES = {
    "CURRENT": RiskProfile.CURRENT_ENFORCED,
    "B0": RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH,
    "B1": RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION,
    "D": RiskProfile.D_EXPERIMENT,
}
IDENTITY_COLUMNS = ("position_id", "close_fill_id", "close_order_id")
STRUCTURAL_TRACE_COLUMNS = (
    "timestamp",
    "event_id",
    "signal_key",
    "accepted",
    "reason_codes",
    "direction",
    "bsp_type",
    "target_position",
    "entry_price",
    "setup_candidate_id",
    "decomposition_id",
    "parent_structure_id",
    "child_match_ids",
    "momentum_status",
)
NO_TUNING_NOTICE = (
    "本报告不改变青派缠论、入场、出场、仓位和成本参数；"
    "只改变冻结的 OPEN-005 风险门控 profile、恢复状态机与 fold 状态边界。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--profiles", nargs="+", default=["CURRENT", "B0", "B1"])
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=12)
    parser.add_argument("--step-months", type=int, default=12)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--identity-runs", type=int, default=2)
    parser.add_argument("--identity-profile", default="CURRENT")
    parser.add_argument("--skip-folds", action="store_true")
    parser.add_argument(
        "--fold-profiles",
        nargs="+",
        default=None,
        help="Profiles that should run fold-reset diagnostics (default: all requested).",
    )
    parser.add_argument("--replay-start", default=None)
    parser.add_argument("--replay-end", default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.identity_runs < 1:
        raise ValueError("identity-runs must be >= 1")

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = _path(args.output_dir) / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    config_path = _path(args.config)
    base_config = load_config(config_path)
    production = load_rb_production_frames(base_config)
    start = _optional_timestamp(args.replay_start)
    end = _optional_timestamp(args.replay_end)
    current = _range(prepare_ohlc_frame(production.current), start, end)
    parent = _range(prepare_ohlc_frame(production.parent), start, end)
    child = _range(prepare_ohlc_frame(production.child), start, end)

    folds = [
        asdict(value)
        for value in ProductionWalkforwardRunner(
            base_config,
            train_months=args.train_months,
            test_months=args.test_months,
            step_months=args.step_months,
            max_folds=args.max_folds,
        ).folds(current)
    ]
    requested = [_profile(value) for value in args.profiles]
    fold_profiles = (
        {_profile(value) for value in args.fold_profiles}
        if args.fold_profiles is not None
        else set(requested)
    )
    identity_profile = _profile(args.identity_profile)
    results: dict[str, dict[str, Any]] = {}
    for profile in requested:
        profile_config = replace(
            base_config,
            risk=replace(base_config.risk, profile=profile),
        )
        results[profile.value] = run_profile(
            profile_config,
            current=current,
            parent=parent,
            child=child,
            folds=folds,
            output_dir=output_root / profile.value,
            identity_runs=(
                args.identity_runs if profile == identity_profile else 1
            ),
            resume=args.resume,
            skip_folds=args.skip_folds or profile not in fold_profiles,
        )

    if RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH in requested:
        disabled_config = replace(
            base_config,
            risk=replace(
                base_config.risk,
                profile=RiskProfile.CURRENT_ENFORCED,
                max_loss_points_mode=RiskGateMode.DISABLED,
                daily_loss_limit_mode=RiskGateMode.DISABLED,
                max_consecutive_losses_mode=RiskGateMode.DISABLED,
                max_drawdown_pct_mode=RiskGateMode.DISABLED,
            ),
        )
        reference_dir = output_root / "B0_DISABLED_REFERENCE"
        reference = run_continuous(
            disabled_config,
            current,
            parent,
            child,
            reference_dir,
            resume=args.resume,
        )
        b0_dir = output_root / RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH.value
        b0_parity = compare_behavior_sources(
            b0_dir / "continuous_run_1",
            reference,
            label="B0_vs_four_gates_disabled",
        )
        _write_json(b0_dir / "b0_disabled_reference_parity.json", b0_parity)
        results[RiskProfile.B0_UNINTERRUPTED_STRATEGY_PATH.value][
            "disabled_reference_parity"
        ] = b0_parity

    current_baseline = (
        PROJECT_ROOT
        / "reports"
        / "rule_mechanism_audit"
        / "20260813_open005_risk_accounting"
        / "source"
    )
    if RiskProfile.CURRENT_ENFORCED in requested and current_baseline.exists():
        current_dir = output_root / RiskProfile.CURRENT_ENFORCED.value
        parity = compare_behavior_sources(
            current_baseline,
            current_dir / "continuous_run_1",
            label="CURRENT_vs_OPEN005_phase1",
        )
        _write_json(current_dir / "current_behavior_parity.json", parity)
        results[RiskProfile.CURRENT_ENFORCED.value]["phase1_parity"] = parity

    if {
        RiskProfile.CURRENT_ENFORCED,
        RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION,
    }.issubset(requested):
        current_dir = output_root / RiskProfile.CURRENT_ENFORCED.value
        b1_dir = output_root / RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value
        current_b1 = compare_profile_paths(
            current_dir / "continuous_run_1",
            b1_dir / "continuous_run_1",
            left_modes=results[RiskProfile.CURRENT_ENFORCED.value]["gate_modes"],
            right_modes=results[
                RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value
            ]["gate_modes"],
        )
        _write_json(output_root / "current_b1_path_delta.json", current_b1)
        results[RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value][
            "current_delta"
        ] = current_b1

    if {
        RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION,
        RiskProfile.D_EXPERIMENT,
    }.issubset(requested):
        b1_dir = output_root / RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value
        d_dir = output_root / RiskProfile.D_EXPERIMENT.value
        d_b1 = compare_d_b1_paths(
            b1_dir / "continuous_run_1",
            d_dir / "continuous_run_1",
            b1_modes=results[
                RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value
            ]["gate_modes"],
            d_modes=results[RiskProfile.D_EXPERIMENT.value]["gate_modes"],
        )
        _write_json(output_root / "d_b1_path_delta.json", d_b1)
        results[RiskProfile.D_EXPERIMENT.value]["b1_delta"] = d_b1
        if (
            not args.skip_folds
            and RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION in fold_profiles
            and RiskProfile.D_EXPERIMENT in fold_profiles
        ):
            d_b1_folds = compare_d_b1_fold_paths(
                b1_dir / "fold_reset",
                d_dir / "fold_reset",
                b1_modes=results[
                    RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value
                ]["gate_modes"],
                d_modes=results[RiskProfile.D_EXPERIMENT.value]["gate_modes"],
            )
            _write_json(output_root / "d_b1_fold_delta.json", d_b1_folds)
            results[RiskProfile.D_EXPERIMENT.value][
                "b1_fold_delta"
            ] = d_b1_folds

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": _git("rev-parse", "HEAD"),
        "git_status_short": _git("status", "--porcelain"),
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "profiles": [value.value for value in requested],
        "identity_runs": args.identity_runs,
        "identity_profile": identity_profile.value,
        "skip_folds": args.skip_folds,
        "fold_profiles": [
            value.value for value in requested if value in fold_profiles
        ],
        "folds": [{key: str(value) for key, value in fold.items()} for fold in folds],
        "frame": {
            "start": str(current["datetime"].min()),
            "end": str(current["datetime"].max()),
            "rows": len(current),
        },
        "no_tuning_notice": NO_TUNING_NOTICE,
        "results": results,
    }
    _write_json(output_root / "phase2_summary.json", meta)
    write_summary(output_root, meta)
    print(f"OPEN-005 phase 2 report written to: {output_root}")
    return 0


def run_profile(
    config,
    *,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    folds: list[dict[str, Any]],
    output_dir: Path,
    identity_runs: int,
    resume: bool,
    skip_folds: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    continuous_dirs: list[Path] = []
    for index in range(1, identity_runs + 1):
        destination = output_dir / f"continuous_run_{index}"
        continuous_dirs.append(
            run_continuous(
                config,
                current,
                parent,
                child,
                destination,
                resume=resume,
            )
        )

    identity = (
        compare_identity_sources(continuous_dirs[0], continuous_dirs[1])
        if len(continuous_dirs) > 1
        else {"exact": None, "reason": "only_one_identity_run"}
    )
    _write_json(output_dir / "identity_parity.json", identity)
    continuous_frames = load_source(continuous_dirs[0])
    if skip_folds:
        fold_frames = {
            name: pd.DataFrame()
            for name in ("decision_trace", "execution_decisions", "trades", "fills")
        }
        fold_rows = []
        continuous_oos = fold_frames
        decision_parity = {
            "exact": None,
            "reason": "folds_skipped",
        }
    else:
        fold_frames, fold_rows = run_folds(
            config,
            current=current,
            parent=parent,
            child=child,
            folds=folds,
            output_dir=output_dir / "fold_reset",
            resume=resume,
        )
        continuous_oos = filter_fold_windows(continuous_frames, folds)
        decision_parity = compare_decisions(continuous_oos, fold_frames)
    fold_path_delta = (
        compare_fold_paths(continuous_oos, fold_frames)
        if not skip_folds
        else {"exact": None, "reason": "folds_skipped"}
    )
    summary = {
        "profile": config.risk.profile.value,
        "gate_modes": {
            gate: mode.value for gate, mode in config.risk.gate_modes().items()
        },
        "continuous_full": summarize(continuous_frames),
        "continuous_oos": summarize(continuous_oos),
        "fold_reset_oos": summarize(fold_frames),
        "fold_rows": fold_rows,
        "decision_parity": decision_parity,
        "continuous_vs_fold_paths": fold_path_delta,
        "identity_parity": identity,
    }
    _write_json(output_dir / "profile_summary.json", summary)
    return summary


def run_continuous(
    config,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    output_dir: Path,
    *,
    resume: bool,
) -> Path:
    if resume and _source_complete(output_dir):
        print(f"reuse continuous: {output_dir}")
        return output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"start continuous {config.risk.profile.value}: {output_dir}", flush=True)
    result = run_backtest(
        config,
        frame=current,
        parent_frame=parent,
        child_frame=child,
    )
    result.save(output_dir)
    _write_json(
        output_dir / "_complete.json",
        {
            "profile": config.risk.profile.value,
            "decision_count": len(result.decision_trace),
            "execution_decision_count": len(result.execution_decisions),
            "trade_count": len(result.trades),
            "fill_count": len(result.fills),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
    )
    print(
        f"completed continuous {config.risk.profile.value}: "
        f"decisions={len(result.decision_trace)} trades={len(result.trades)} "
        f"elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )
    return output_dir


def run_folds(
    config,
    *,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    folds: list[dict[str, Any]],
    output_dir: Path,
    resume: bool,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    collected = {name: [] for name in ("decision_trace", "execution_decisions", "trades", "fills")}
    rows: list[dict[str, Any]] = []
    for fold in folds:
        fold_number = int(fold["fold"])
        fold_dir = output_dir / f"fold_{fold_number:02d}"
        if not (resume and _fold_source_complete(fold_dir, fold)):
            mask = (
                (current["datetime"] >= fold["test_start"])
                & (current["datetime"] < fold["test_end"])
            )
            indices = current.index[mask]
            if len(indices) == 0:
                continue
            warmup_index = _replay_start_index(
                current,
                int(indices[0]),
                config.production.warmup_bars,
            )
            replay_start = current.iloc[warmup_index]["datetime"]
            post_test = current.index[current["datetime"] >= fold["test_end"]]
            if len(post_test):
                warmdown_end_index = min(
                    len(current) - 1,
                    int(post_test[0]) + config.production.warmup_bars,
                )
            else:
                warmdown_end_index = len(current) - 1
            replay_end = current.iloc[warmdown_end_index]["datetime"]
            result = run_backtest(
                config,
                frame=_slice(current, replay_start, replay_end, inclusive=True),
                parent_frame=_slice(parent, replay_start, replay_end, inclusive=True),
                child_frame=_slice(child, replay_start, replay_end, inclusive=True),
                decision_start=fold["test_start"],
                decision_end=fold["test_end"],
            )
            result.save(fold_dir)
            _write_json(
                fold_dir / "_complete.json",
                {
                    "profile": config.risk.profile.value,
                    "fold": fold_number,
                    "decision_count": len(result.decision_trace),
                    "execution_decision_count": len(result.execution_decisions),
                    "trade_count": len(result.trades),
                    "fill_count": len(result.fills),
                    "warmdown_policy": "natural_exit_v1",
                    "warmdown_bars": config.production.warmup_bars,
                    "replay_end": str(replay_end),
                },
            )
            if not _fold_source_complete(fold_dir, fold):
                (fold_dir / "_complete.json").unlink(missing_ok=True)
                raise RuntimeError(
                    f"fold {fold_number:02d} contains an in-window position "
                    "forced out by end_of_data; increase the warmdown horizon"
                )
        frames = filter_fold_windows(load_source(fold_dir), [fold])
        for name, frame in frames.items():
            if not frame.empty:
                frame = frame.copy()
                frame["fold"] = fold_number
            collected[name].append(frame)
        fold_summary = summarize(frames)
        rows.append({**{key: str(value) for key, value in fold.items()}, **fold_summary})
        print(f"completed {config.risk.profile.value} fold {fold_number:02d}")

    combined = {name: _concat(values) for name, values in collected.items()}
    for name, frame in combined.items():
        if not frame.empty:
            frame.to_csv(output_dir / f"all_{name}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rows).to_csv(output_dir / "fold_summary.csv", index=False, encoding="utf-8-sig")
    return combined, rows


def load_source(path: Path) -> dict[str, pd.DataFrame]:
    return {
        "decision_trace": _read(path / "decision_trace.csv"),
        "execution_decisions": _read(path / "execution_decisions.csv"),
        "trades": _read(path / "trades.csv"),
        "fills": _read(path / "fills.csv"),
    }


def summarize(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    decisions = frames["decision_trace"]
    execution = frames["execution_decisions"]
    trades = frames["trades"]
    fills = frames["fills"]
    accepted = (
        decisions[decisions["accepted"].map(_bool)]
        if not decisions.empty and "accepted" in decisions
        else pd.DataFrame()
    )
    rejected = (
        execution[execution["status"] == "risk_rejected"]
        if not execution.empty and "status" in execution
        else pd.DataFrame()
    )
    pnl = pd.to_numeric(trades.get("pnl_points", pd.Series(dtype=float)), errors="coerce")
    return {
        "decision_count": len(decisions),
        "accepted_count": len(accepted),
        "execution_decision_count": len(execution),
        "risk_rejected_count": len(rejected),
        "trade_count": len(trades),
        "fill_count": len(fills),
        "net_pnl_points": round(float(pnl.sum()), 4) if len(pnl) else 0.0,
        "status_counts": _counts(execution, "status"),
        "rejection_reasons": _counts(rejected, "rejection_reason"),
        "enforced_breaches": _json_array_counts(execution, "risk_enforced_breaches"),
        "observed_breaches": _json_array_counts(execution, "risk_observed_breaches"),
        "hard_failures": _json_array_counts(execution, "risk_hard_failures"),
        "first_trade_time": _time(trades, "entry_time", "min"),
        "last_trade_time": _time(trades, "entry_time", "max"),
    }


def compare_identity_sources(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = _read(left_path / "trades.csv")
    right = _read(right_path / "trades.csv")
    if left.empty and right.empty:
        return {
            "exact": True,
            "evidence_sufficient": False,
            "left_rows": 0,
            "right_rows": 0,
            "differing_rows": 0,
            "reason": "zero_trade_identity_comparison",
        }
    missing = [column for column in IDENTITY_COLUMNS if column not in left or column not in right]
    if missing:
        return {
            "exact": False,
            "evidence_sufficient": False,
            "missing_columns": missing,
        }
    left_ids = left[list(IDENTITY_COLUMNS)].fillna("").astype(str).reset_index(drop=True)
    right_ids = right[list(IDENTITY_COLUMNS)].fillna("").astype(str).reset_index(drop=True)
    differing = _differing_rows(left_ids, right_ids)
    return {
        "exact": len(left) == len(right) and differing == 0,
        "evidence_sufficient": len(left) > 0 and len(right) > 0,
        "left_rows": len(left),
        "right_rows": len(right),
        "differing_rows": differing,
        "columns": list(IDENTITY_COLUMNS),
    }


def compare_behavior_sources(left_path: Path, right_path: Path, *, label: str) -> dict[str, Any]:
    left = load_source(left_path)
    right = load_source(right_path)
    result: dict[str, Any] = {"label": label, "files": {}}
    for name in left:
        first = left[name]
        second = right[name]
        excluded = {
            "decision_id",
            "risk_profile",
            "risk_gate_modes",
            "risk_hard_failures",
            "risk_enforced_breaches",
            "risk_observed_breaches",
            "risk_evaluated_gates",
        }
        common = sorted((set(first.columns) & set(second.columns)) - excluded)
        differing = _differing_rows(
            first[common].fillna("").astype(str).reset_index(drop=True),
            second[common].fillna("").astype(str).reset_index(drop=True),
        )
        result["files"][name] = {
            "left_rows": len(first),
            "right_rows": len(second),
            "common_columns": common,
            "differing_rows": differing,
            "exact": len(first) == len(second) and differing == 0,
        }
    result["exact"] = all(value["exact"] for value in result["files"].values())
    return result


def compare_profile_paths(
    left_path: Path,
    right_path: Path,
    *,
    left_modes: dict[str, str],
    right_modes: dict[str, str],
) -> dict[str, Any]:
    left = load_source(left_path)
    right = load_source(right_path)
    mode_differences = {
        gate: {"left": left_modes.get(gate), "right": right_modes.get(gate)}
        for gate in sorted(set(left_modes) | set(right_modes))
        if left_modes.get(gate) != right_modes.get(gate)
    }
    account_state_trace = compare_behavior_sources(
        left_path,
        right_path,
        label="CURRENT_vs_B1",
    )["files"]["decision_trace"]
    structural_columns = [
        column
        for column in STRUCTURAL_TRACE_COLUMNS
        if column in left["decision_trace"] and column in right["decision_trace"]
    ]
    structural_differences = _differing_rows(
        left["decision_trace"][structural_columns]
        .fillna("")
        .astype(str)
        .reset_index(drop=True),
        right["decision_trace"][structural_columns]
        .fillna("")
        .astype(str)
        .reset_index(drop=True),
    )
    left_summary = summarize(left)
    right_summary = summarize(right)
    return {
        "experimental_variable_isolated": mode_differences
        == {
            "max_consecutive_losses": {
                "left": RiskGateMode.ENFORCE.value,
                "right": RiskGateMode.DISABLED.value,
            }
        },
        "gate_mode_differences": mode_differences,
        "structural_trace": {
            "exact": (
                len(left["decision_trace"]) == len(right["decision_trace"])
                and structural_differences == 0
            ),
            "left_rows": len(left["decision_trace"]),
            "right_rows": len(right["decision_trace"]),
            "differing_rows": structural_differences,
            "columns": structural_columns,
        },
        "account_state_trace": account_state_trace,
        "outcome_delta": {
            "execution_decisions": (
                right_summary["execution_decision_count"]
                - left_summary["execution_decision_count"]
            ),
            "risk_rejections": (
                right_summary["risk_rejected_count"]
                - left_summary["risk_rejected_count"]
            ),
            "trades": right_summary["trade_count"] - left_summary["trade_count"],
            "fills": right_summary["fill_count"] - left_summary["fill_count"],
            "net_pnl_points": round(
                right_summary["net_pnl_points"] - left_summary["net_pnl_points"],
                4,
            ),
        },
        "first_execution_divergence": _first_execution_divergence(
            left["execution_decisions"],
            right["execution_decisions"],
        ),
    }


def compare_d_b1_paths(
    b1_path: Path,
    d_path: Path,
    *,
    b1_modes: dict[str, str],
    d_modes: dict[str, str],
) -> dict[str, Any]:
    return _compare_d_b1_frames(
        load_source(b1_path),
        load_source(d_path),
        b1_modes=b1_modes,
        d_modes=d_modes,
        comparison_scope="continuous",
    )


def compare_d_b1_fold_paths(
    b1_path: Path,
    d_path: Path,
    *,
    b1_modes: dict[str, str],
    d_modes: dict[str, str],
) -> dict[str, Any]:
    return _compare_d_b1_frames(
        _load_combined_fold_source(b1_path),
        _load_combined_fold_source(d_path),
        b1_modes=b1_modes,
        d_modes=d_modes,
        comparison_scope="fold_reset_oos",
    )


def _compare_d_b1_frames(
    b1: dict[str, pd.DataFrame],
    d: dict[str, pd.DataFrame],
    *,
    b1_modes: dict[str, str],
    d_modes: dict[str, str],
    comparison_scope: str,
) -> dict[str, Any]:
    b1_summary = summarize(b1)
    d_summary = summarize(d)
    structural = _compare_keyed_frames(
        b1["decision_trace"],
        d["decision_trace"],
        keys=["timestamp", "event_id"],
        columns=[
            column
            for column in STRUCTURAL_TRACE_COLUMNS
            if column in b1["decision_trace"] and column in d["decision_trace"]
        ],
    )
    d_execution = d["execution_decisions"]
    probe_trades = (
        int(d["trades"].get("is_option_d_probe", pd.Series(dtype=bool)).map(_bool).sum())
        if not d["trades"].empty
        else 0
    )
    return {
        "comparison_scope": comparison_scope,
        "direct_control": RiskProfile.B1_CONSECUTIVE_LOSS_ABLATION.value,
        "experimental_profile": RiskProfile.D_EXPERIMENT.value,
        "experimental_variable_isolated": b1_modes == d_modes,
        "gate_modes_exact": b1_modes == d_modes,
        "gate_modes": d_modes,
        "only_policy_difference": "OPEN005-D-001 consecutive-loss probe regime",
        "structural_trace": structural,
        "probe_trade_count": probe_trades,
        "option_d_status_counts": (
            _counts(d_execution, "option_d_regime_state")
            if not d_execution.empty
            else {}
        ),
        "outcome_delta": {
            "execution_decisions": (
                d_summary["execution_decision_count"]
                - b1_summary["execution_decision_count"]
            ),
            "risk_rejections": (
                d_summary["risk_rejected_count"]
                - b1_summary["risk_rejected_count"]
            ),
            "trades": d_summary["trade_count"] - b1_summary["trade_count"],
            "fills": d_summary["fill_count"] - b1_summary["fill_count"],
            "net_pnl_points": round(
                d_summary["net_pnl_points"] - b1_summary["net_pnl_points"],
                4,
            ),
        },
        "first_execution_divergence": _first_execution_divergence(
            b1["execution_decisions"],
            d["execution_decisions"],
        ),
    }


def _load_combined_fold_source(path: Path) -> dict[str, pd.DataFrame]:
    return {
        name: _read(path / f"all_{name}.csv")
        for name in ("decision_trace", "execution_decisions", "trades", "fills")
    }


def _first_execution_divergence(
    left: pd.DataFrame,
    right: pd.DataFrame,
) -> dict[str, Any] | None:
    columns = ["timestamp", "event_id", "status", "rejection_reason"]
    available = [column for column in columns if column in left and column in right]
    common = min(len(left), len(right))
    for index in range(common):
        left_row = left.iloc[index]
        right_row = right.iloc[index]
        if any(str(left_row[column]) != str(right_row[column]) for column in available):
            return {
                "row": index,
                "left": {column: _json_value(left_row[column]) for column in available},
                "right": {column: _json_value(right_row[column]) for column in available},
            }
    if len(left) != len(right):
        frame = left if len(left) > common else right
        side = "left" if len(left) > common else "right"
        row = frame.iloc[common]
        return {
            "row": common,
            "only_in": side,
            side: {column: _json_value(row[column]) for column in available},
        }
    return None


def compare_decisions(left: dict[str, pd.DataFrame], right: dict[str, pd.DataFrame]) -> dict[str, Any]:
    first = left["decision_trace"].copy()
    second = right["decision_trace"].copy()
    keys = ["timestamp", "event_id"]
    for frame in (first, second):
        if not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    merged = first[keys + ["accepted"]].merge(
        second[keys + ["accepted"]],
        on=keys,
        how="outer",
        suffixes=("_continuous", "_fold"),
        indicator=True,
    )
    both = merged["_merge"].eq("both")
    mismatch = both & merged["accepted_continuous"].map(_bool).ne(
        merged["accepted_fold"].map(_bool)
    )
    return {
        "exact": bool(merged["_merge"].eq("both").all() and not mismatch.any()),
        "continuous_count": len(first),
        "fold_count": len(second),
        "continuous_only": int(merged["_merge"].eq("left_only").sum()),
        "fold_only": int(merged["_merge"].eq("right_only").sum()),
        "accepted_mismatch": int(mismatch.sum()),
    }


def compare_fold_paths(
    continuous: dict[str, pd.DataFrame],
    fold_reset: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    specifications = {
        "decision_trace": (
            ("timestamp", "event_id"),
            STRUCTURAL_TRACE_COLUMNS,
        ),
        "execution_decisions": (
            ("timestamp", "event_id"),
            (
            "timestamp",
            "event_id",
            "status",
            "rejection_stage",
            "rejection_reason",
            "action",
            "target_position",
            "fill_price",
            "risk_hard_failures",
            "risk_enforced_breaches",
            "risk_observed_breaches",
            ),
        ),
        "trades": (
            ("entry_time", "event_id"),
            (
            "entry_time",
            "exit_time",
            "event_id",
            "direction",
            "entry_price",
            "exit_price",
            "pnl_points",
            "exit_reason",
            "setup_candidate_id",
            ),
        ),
        "fills": (
            ("timestamp", "action"),
            (
            "timestamp",
            "action",
            "previous_position",
            "target_position",
            "quantity_delta",
            "signal_price",
            "fill_price",
            ),
        ),
    }
    result: dict[str, Any] = {}
    for name, (keys, requested) in specifications.items():
        left = continuous[name]
        right = fold_reset[name]
        common = [column for column in requested if column in left and column in right]
        result[name] = _compare_keyed_frames(
            left,
            right,
            keys=[column for column in keys if column in common],
            columns=common,
        )
    result["exact"] = all(value["exact"] for value in result.values())
    return result


def filter_fold_windows(frames: dict[str, pd.DataFrame], folds: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    columns = {
        "decision_trace": "timestamp",
        "execution_decisions": "timestamp",
        "trades": "entry_time",
        "fills": "timestamp",
    }
    result = {}
    for name, frame in frames.items():
        if name == "fills":
            result[name] = _filter_fill_lifecycles(frame, folds)
            continue
        column = columns[name]
        if frame.empty or column not in frame:
            result[name] = frame.copy()
            continue
        values = pd.to_datetime(frame[column])
        mask = pd.Series(False, index=frame.index)
        for fold in folds:
            mask |= (values >= fold["test_start"]) & (values < fold["test_end"])
        result[name] = frame.loc[mask].reset_index(drop=True)
    return result


def write_summary(output_dir: Path, meta: dict[str, Any]) -> None:
    profiles = set(meta["profiles"])
    has_d = RiskProfile.D_EXPERIMENT.value in profiles
    lines = [
        "# OPEN-005 风险门控与恢复状态诊断",
        "",
        NO_TUNING_NOTICE,
        "",
        "## 运行范围",
        "",
        f"- 数据：{meta['frame']['start']} 至 {meta['frame']['end']}，{meta['frame']['rows']} bars。",
        f"- Profiles：{', '.join(meta['profiles'])}。",
        f"- 标准 folds：{len(meta['folds'])}。",
        "",
        "## 汇总",
        "",
        "| Profile | 连续交易 | 连续净点数 | 连续拒绝 | 连续 OOS 交易 | Fold OOS 交易 | 身份双跑 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for profile, result in meta["results"].items():
        full = result["continuous_full"]
        oos = result["continuous_oos"]
        fold = result["fold_reset_oos"]
        identity = result["identity_parity"].get("exact")
        lines.append(
            f"| {profile} | {full['trade_count']} | {full['net_pnl_points']} | "
            f"{full['risk_rejected_count']} | {oos['trade_count']} | "
            f"{fold['trade_count']} | {identity} |"
        )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- B0 是四个状态门控 observe，不是纯 Alpha；硬前提仍阻断。",
            "- B1 只 disabled 连亏门控，其他门控继续 enforce。",
            "- Fold-reset 是状态敏感性诊断，不是可直接交易的收益口径。",
        ]
    )
    if has_d:
        lines.extend(
            [
                "- Option D 只与 B1 比较；两者门控模式相同，差异限于冻结的暂停/探针恢复状态机。",
                "- Option D 结果是研究诊断，不代表已经批准替换现行风险规则。",
            ]
        )
    else:
        lines.append("- Option D 未参与本次运行。")
    (output_dir / "phase2_summary.md").write_text("\n".join(lines), encoding="utf-8")


def _source_complete(path: Path) -> bool:
    return (path / "_complete.json").exists()


def _fold_source_complete(path: Path, fold: dict[str, Any]) -> bool:
    marker_path = path / "_complete.json"
    if not marker_path.exists():
        return False
    trades = _read(path / "trades.csv")
    if trades.empty or "exit_reason" not in trades:
        return True
    entry_time = pd.to_datetime(trades.get("entry_time"), errors="coerce")
    in_window = (entry_time >= fold["test_start"]) & (entry_time < fold["test_end"])
    return not trades.loc[in_window, "exit_reason"].eq("end_of_data").any()


def _range(frame: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    result = frame
    if start is not None:
        result = result[result["datetime"] >= start]
    if end is not None:
        result = result[result["datetime"] < end]
    return result.reset_index(drop=True)


def _profile(value: str) -> RiskProfile:
    try:
        return PROFILE_ALIASES[str(value).upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported profile alias: {value}") from exc


def _path(value: Path) -> Path:
    return value if value.is_absolute() else PROJECT_ROOT / value


def _optional_timestamp(value: str | None) -> pd.Timestamp | None:
    return pd.Timestamp(value, tz="Asia/Shanghai") if value else None


def _read(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    values = [frame for frame in frames if not frame.empty]
    return pd.concat(values, ignore_index=True) if values else pd.DataFrame()


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    item = getattr(value, "item", None)
    return item() if callable(item) else value


def _counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if frame.empty or column not in frame:
        return {}
    return {str(key): int(value) for key, value in frame[column].fillna("").value_counts().items()}


def _json_array_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if frame.empty or column not in frame:
        return counts
    for raw in frame[column].fillna("[]"):
        try:
            values = json.loads(str(raw))
        except json.JSONDecodeError:
            values = []
        for value in values:
            key = str(value).split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
    return counts


def _time(frame: pd.DataFrame, column: str, operation: str) -> str | None:
    if frame.empty or column not in frame:
        return None
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    return str(getattr(values, operation)())


def _differing_rows(left: pd.DataFrame, right: pd.DataFrame) -> int:
    common = min(len(left), len(right))
    differing = abs(len(left) - len(right))
    if common:
        differing += int(left.iloc[:common].ne(right.iloc[:common]).any(axis=1).sum())
    return differing


def _compare_keyed_frames(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    keys: list[str],
    columns: list[str],
) -> dict[str, Any]:
    if not keys:
        differing = _differing_rows(
            left[columns].fillna("").astype(str).reset_index(drop=True),
            right[columns].fillna("").astype(str).reset_index(drop=True),
        )
        return {
            "exact": len(left) == len(right) and differing == 0,
            "continuous_rows": len(left),
            "fold_rows": len(right),
            "continuous_only": max(0, len(left) - len(right)),
            "fold_only": max(0, len(right) - len(left)),
            "differing_rows": differing,
            "keys": [],
            "columns": columns,
        }

    payload = [column for column in columns if column not in keys]
    prepared = []
    for frame in (left, right):
        value = frame[columns].copy().fillna("").astype(str)
        value["_key_occurrence"] = value.groupby(keys, dropna=False).cumcount()
        prepared.append(value)
    merged = prepared[0].merge(
        prepared[1],
        on=[*keys, "_key_occurrence"],
        how="outer",
        suffixes=("_continuous", "_fold"),
        indicator=True,
    )
    both = merged["_merge"].eq("both")
    mismatch = pd.Series(False, index=merged.index)
    for column in payload:
        mismatch |= both & merged[f"{column}_continuous"].ne(
            merged[f"{column}_fold"]
        )
    continuous_only = int(merged["_merge"].eq("left_only").sum())
    fold_only = int(merged["_merge"].eq("right_only").sum())
    differing = int(mismatch.sum())
    return {
        "exact": not continuous_only and not fold_only and not differing,
        "continuous_rows": len(left),
        "fold_rows": len(right),
        "continuous_only": continuous_only,
        "fold_only": fold_only,
        "differing_rows": differing,
        "keys": keys,
        "columns": columns,
    }


def _filter_fill_lifecycles(
    frame: pd.DataFrame,
    folds: list[dict[str, Any]],
) -> pd.DataFrame:
    if frame.empty or "timestamp" not in frame:
        return frame.copy()
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    selected: list[int] = []
    include_position = False
    for index, row in frame.iterrows():
        previous = int(row.get("previous_position", 0) or 0)
        target = int(row.get("target_position", 0) or 0)
        if previous == 0 and target != 0:
            timestamp = timestamps.loc[index]
            include_position = any(
                fold["test_start"] <= timestamp < fold["test_end"]
                for fold in folds
            )
        if include_position:
            selected.append(index)
        if target == 0:
            include_position = False
    return frame.loc[selected].reset_index(drop=True)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
