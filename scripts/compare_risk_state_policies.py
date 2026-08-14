"""Compare continuous risk state vs fold-reset risk state for Qingpai RB.

This is a reporting script, not an optimizer.  It keeps strategy parameters
fixed and changes only the research accounting boundary for stateful risk
counters such as max_consecutive_losses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
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


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "rb_15m_qingpai_strict.yaml"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "risk_state_policy_compare"
NO_TUNING_NOTICE = (
    "本报告不调整策略参数，不输出 hypothetical PnL；"
    "连续流与 fold 重放的决策同一性另行审计。"
)
RISK_DRAWDOWN_BASIS = (
    "账户权益 = 初始资金 + 按市价计值的累计盈亏点数 × 合约乘数；"
    "峰值从初始资金开始并逐 K 线更新。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--continuous-source-dir", type=Path, default=None)
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=12)
    parser.add_argument("--step-months", type=int, default=12)
    parser.add_argument("--max-folds", type=int, default=None)
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (args.output_dir if args.output_dir.is_absolute() else PROJECT_ROOT / args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = args.config if args.config.is_absolute() else PROJECT_ROOT / args.config
    config = load_config(config_path)
    frames = load_rb_production_frames(config)
    current = prepare_ohlc_frame(frames.current).reset_index(drop=True)
    parent = prepare_ohlc_frame(frames.parent).reset_index(drop=True)
    child = prepare_ohlc_frame(frames.child).reset_index(drop=True)

    runner = ProductionWalkforwardRunner(
        config,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        max_folds=args.max_folds,
    )
    folds = runner.folds(current)
    fold_dicts = [asdict(fold) for fold in folds]

    continuous_source = _resolve_continuous_source(args.continuous_source_dir, output_dir)
    continuous_frames = load_source_frames(continuous_source)
    continuous_full = summarize_policy_frames(
        "continuous_full_history",
        continuous_frames,
    )
    continuous_oos_frames = filter_source_frames(continuous_frames, fold_dicts)
    continuous_oos = summarize_policy_frames(
        "continuous_standard_oos_windows",
        continuous_oos_frames,
    )

    fold_reset_frames, fold_rows = run_fold_reset(
        config=config,
        current=current,
        parent=parent,
        child=child,
        folds=fold_dicts,
        output_dir=output_dir / "fold_reset",
    )
    fold_reset_summary = summarize_policy_frames(
        "fold_reset_standard_oos_windows",
        fold_reset_frames,
    )
    parity, parity_diff = audit_decision_parity(
        continuous_oos_frames,
        fold_reset_frames,
    )

    comparison = pd.DataFrame([continuous_full, continuous_oos, fold_reset_summary])
    comparison.to_csv(output_dir / "risk_state_policy_comparison.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(fold_rows).to_csv(output_dir / "fold_reset_summary.csv", index=False, encoding="utf-8-sig")
    parity_diff.to_csv(
        output_dir / "decision_parity_diff.csv",
        index=False,
        encoding="utf-8-sig",
    )

    write_report(
        output_dir,
        comparison,
        fold_rows,
        continuous_source,
        fold_dicts,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        parity=parity,
    )
    write_drawdown_audit(
        output_dir=output_dir,
        fold_rows=fold_rows,
        execution=fold_reset_frames.get("execution_decisions", pd.DataFrame()),
        initial_capital=config.sizing.capital,
        contract_multiplier=config.execution.contract_multiplier,
        threshold=config.risk.max_drawdown_pct,
        parity=parity,
    )
    write_meta(
        output_dir=output_dir,
        config_path=config_path,
        current=current,
        parent=parent,
        child=child,
        continuous_source=continuous_source,
        folds=fold_dicts,
        parity=parity,
    )

    print(f"Risk-state policy comparison written to: {output_dir}")
    print(comparison.to_string(index=False))
    return 0


def run_fold_reset(
    *,
    config,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    folds: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_decisions = []
    all_execution = []
    all_trades = []
    all_fills = []
    fold_rows: list[dict[str, Any]] = []

    for fold in folds:
        test_mask = (
            (current["datetime"] >= fold["test_start"])
            & (current["datetime"] < fold["test_end"])
        )
        test_indices = current.index[test_mask]
        if len(test_indices) == 0:
            continue
        warmup_start_idx = _replay_start_index(
            current,
            int(test_indices[0]),
            config.production.warmup_bars,
        )
        replay_start = current.iloc[warmup_start_idx]["datetime"]
        replay_end = fold["test_end"]
        result = run_backtest(
            config,
            frame=_slice(current, replay_start, replay_end),
            parent_frame=_slice(parent, replay_start, replay_end),
            child_frame=_slice(child, replay_start, replay_end),
            decision_start=fold["test_start"],
            decision_end=fold["test_end"],
        )

        fold_dir = output_dir / f"fold_{int(fold['fold']):02d}"
        result.save(fold_dir)

        decisions = pd.DataFrame([record.to_dict() for record in result.decision_trace])
        execution = pd.DataFrame(result.execution_decisions)
        trades = pd.DataFrame(result.trades)
        fills = result.fills.copy()
        for frame in (decisions, execution, trades, fills):
            if not frame.empty:
                frame["fold"] = int(fold["fold"])

        all_decisions.append(decisions)
        all_execution.append(execution)
        all_trades.append(trades)
        all_fills.append(fills)
        fold_summary = summarize_policy_frames(
            f"fold_{int(fold['fold']):02d}",
            {
                "decision_trace": decisions,
                "execution_decisions": execution,
                "trades": trades,
                "fills": fills,
            },
        )
        fold_summary.update(
            summarize_account_drawdown(
                result.bars,
                threshold=config.risk.max_drawdown_pct,
            )
        )
        fold_rows.append({**fold, **fold_summary, "replay_start": replay_start})

    combined = {
        "decision_trace": _concat(all_decisions),
        "execution_decisions": _concat(all_execution),
        "trades": _concat(all_trades),
        "fills": _concat(all_fills),
    }
    for name, frame in combined.items():
        if not frame.empty:
            frame.to_csv(output_dir / f"all_{name}.csv", index=False, encoding="utf-8-sig")
    return combined, fold_rows


def summarize_policy_frames(label: str, frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    decisions = frames.get("decision_trace", pd.DataFrame()).copy()
    execution = frames.get("execution_decisions", pd.DataFrame()).copy()
    trades = frames.get("trades", pd.DataFrame()).copy()
    accepted = decisions[decisions["accepted"].map(_to_bool)] if not decisions.empty and "accepted" in decisions.columns else pd.DataFrame()
    risk_rejected = execution[execution["status"] == "risk_rejected"] if not execution.empty and "status" in execution.columns else pd.DataFrame()
    executed = execution[execution["status"].astype(str).str.startswith("executed_", na=False)] if not execution.empty and "status" in execution.columns else pd.DataFrame()
    trade_net = pd.to_numeric(trades.get("pnl_points", pd.Series(dtype=float)), errors="coerce")
    accepted_event_ids = _id_set(accepted, "event_id")
    trade_event_ids = _id_set(trades, "event_id")
    accepted_not_executed = len(accepted_event_ids - trade_event_ids)
    return {
        "policy": label,
        "decision_count": int(len(decisions)),
        "accepted_count": int(len(accepted)),
        "execution_decision_count": int(len(execution)),
        "executed_status_count": int(len(executed)),
        "risk_rejected_count": int(len(risk_rejected)),
        "accepted_not_executed_count": int(accepted_not_executed),
        "trade_count": int(len(trades)),
        "net_pnl_points": round(float(trade_net.sum()), 4) if len(trade_net) else 0.0,
        "long_trade_count": _count_values(trades, "direction", "long"),
        "short_trade_count": _count_values(trades, "direction", "short"),
        "long_accepted_count": _count_values(accepted, "direction", "long"),
        "short_accepted_count": _count_values(accepted, "direction", "short"),
        "status_counts": _value_counts_json(execution, "status"),
        "risk_rejection_reasons": _value_counts_json(risk_rejected, "rejection_reason"),
        "first_decision_time": _min_time(decisions, "timestamp"),
        "last_decision_time": _max_time(decisions, "timestamp"),
        "first_trade_time": _min_time(trades, "entry_time"),
        "last_trade_time": _max_time(trades, "entry_time"),
    }


def load_source_frames(source_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        "decision_trace": _read_csv(source_dir / "decision_trace.csv"),
        "execution_decisions": _read_csv(source_dir / "execution_decisions.csv"),
        "trades": _read_csv(source_dir / "trades.csv"),
        "fills": _read_csv(source_dir / "fills.csv"),
    }


def filter_source_frames(
    frames: dict[str, pd.DataFrame],
    folds: list[dict[str, Any]],
) -> dict[str, pd.DataFrame]:
    return {
        "decision_trace": _filter_windows(frames.get("decision_trace", pd.DataFrame()), "timestamp", folds),
        "execution_decisions": _filter_windows(frames.get("execution_decisions", pd.DataFrame()), "timestamp", folds),
        "trades": _filter_windows(frames.get("trades", pd.DataFrame()), "entry_time", folds),
        "fills": _filter_windows(frames.get("fills", pd.DataFrame()), "timestamp", folds),
    }


def audit_decision_parity(
    continuous_frames: dict[str, pd.DataFrame],
    fold_frames: dict[str, pd.DataFrame],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Compare event decisions before attributing differences to risk state."""
    left = continuous_frames.get("decision_trace", pd.DataFrame()).copy()
    right = fold_frames.get("decision_trace", pd.DataFrame()).copy()
    keys = ["timestamp", "event_id"]
    for frame in (left, right):
        if frame.empty:
            continue
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    columns = keys + ["accepted", "direction", "reason_codes", "policy_id"]
    left = left[[column for column in columns if column in left.columns]]
    right = right[[column for column in columns if column in right.columns]]
    merged = left.merge(
        right,
        on=keys,
        how="outer",
        suffixes=("_continuous", "_fold_reset"),
        indicator=True,
    )
    accepted_left = merged.get("accepted_continuous", pd.Series(index=merged.index, dtype=object)).map(_to_bool)
    accepted_right = merged.get("accepted_fold_reset", pd.Series(index=merged.index, dtype=object)).map(_to_bool)
    shared = merged["_merge"].eq("both")
    accepted_mismatch = shared & accepted_left.ne(accepted_right)
    merged["parity_issue"] = ""
    merged.loc[merged["_merge"].eq("left_only"), "parity_issue"] = "continuous_only"
    merged.loc[merged["_merge"].eq("right_only"), "parity_issue"] = "fold_reset_only"
    merged.loc[accepted_mismatch, "parity_issue"] = "accepted_mismatch"
    diff = merged[merged["parity_issue"].ne("")].reset_index(drop=True)
    summary = {
        "exact": bool(diff.empty),
        "continuous_count": int(len(left)),
        "fold_reset_count": int(len(right)),
        "shared_count": int(shared.sum()),
        "continuous_only_count": int(merged["_merge"].eq("left_only").sum()),
        "fold_reset_only_count": int(merged["_merge"].eq("right_only").sum()),
        "accepted_mismatch_count": int(accepted_mismatch.sum()),
        "accepted_continuous_only_count": int(
            (merged["_merge"].eq("left_only") & accepted_left).sum()
        ),
        "accepted_fold_reset_only_count": int(
            (merged["_merge"].eq("right_only") & accepted_right).sum()
        ),
    }
    return summary, diff


def summarize_account_drawdown(
    bars: pd.DataFrame,
    *,
    threshold: float | None,
) -> dict[str, Any]:
    equity = pd.to_numeric(
        bars.get("account_equity", pd.Series(dtype=float)),
        errors="coerce",
    ).dropna()
    if equity.empty:
        return {
            "max_account_drawdown_pct": None,
            "drawdown_breach_bar_count": 0,
        }
    peak = equity.cummax()
    drawdown = (peak - equity) / peak.where(peak > 0)
    breach_count = 0 if threshold is None else int((drawdown >= threshold).sum())
    return {
        "max_account_drawdown_pct": float(drawdown.max()),
        "drawdown_breach_bar_count": breach_count,
    }


def write_report(
    output_dir: Path,
    comparison: pd.DataFrame,
    fold_rows: list[dict[str, Any]],
    continuous_source: Path,
    folds: list[dict[str, Any]],
    *,
    train_months: int,
    test_months: int,
    step_months: int,
    parity: dict[str, Any],
) -> None:
    rows = {row["policy"]: row for row in comparison.to_dict("records")}
    continuous_oos = rows.get("continuous_standard_oos_windows", {})
    fold_reset = rows.get("fold_reset_standard_oos_windows", {})
    lines = [
        "# 风控状态口径对照回测",
        "",
        NO_TUNING_NOTICE,
        "",
        "## 1. 输入口径",
        "",
        f"- 连续状态 source: `{continuous_source}`",
        f"- Fold 规则: train={train_months}m, test={test_months}m, step={step_months}m, folds={len(folds)}",
        "- 连续 OOS 汇总只统计同一组 fold test windows，避免与 fold-reset 覆盖区间不一致。",
        f"- `max_drawdown_pct` 基准: {RISK_DRAWDOWN_BASIS}",
        "",
        "## 2. 核心对照",
        "",
        _comparison_line("连续状态 OOS", continuous_oos),
        _comparison_line("Fold-reset OOS", fold_reset),
        "",
        "## 3. 主要差异",
        "",
        _delta_line("已成交交易数", continuous_oos, fold_reset, "trade_count"),
        _delta_line("规则 accepted", continuous_oos, fold_reset, "accepted_count"),
        _delta_line("accepted 但未成交", continuous_oos, fold_reset, "accepted_not_executed_count"),
        _delta_line("风控拒绝", continuous_oos, fold_reset, "risk_rejected_count"),
        _delta_line("净点数", continuous_oos, fold_reset, "net_pnl_points"),
        "",
        "## 4. Fold-Reset 分年表现",
        "",
        *_fold_table_lines(fold_rows),
        "",
        "## 5. 新机制观察",
        "",
        _drawdown_note(fold_rows),
        "",
        "## 6. 决策流同一性",
        "",
        _parity_note(parity),
        "",
        "## 7. 正式研究报告格式结论",
        "",
        "- 采用双口径并列。连续状态作为真实持续运行路径的主口径；fold-reset 作为状态敏感性诊断口径。",
        "- 不把两者的净点数差解释为调参收益，也不把它解释为纯风险重置的严格因果收益。",
        "- 每份正式报告必须同时列出决策同一性、风控拒绝原因和逐 bar 账户最大回撤。",
        "",
        "## 8. 解释",
        "",
        "- 连续状态口径回答：如果风控状态从 2018 年开始一直连续滚动，策略会怎样。",
        "- Fold-reset 口径回答：如果每个研究 fold 独立启动风控状态，规则在各 OOS 阶段会怎样。",
        "- 两者差异不代表调参收益，只用于分离策略规则问题与状态熔断问题。",
        "",
        "## 9. 输出文件",
        "",
        "- `risk_state_policy_comparison.csv`: 三种口径总表。",
        "- `fold_reset_summary.csv`: 每个 fold 的独立风控状态结果。",
        "- `fold_reset/fold_XX/`: 每个 fold 的原始回测产物。",
        "- `fold_reset/all_execution_decisions.csv`: fold-reset 聚合执行/风控审计。",
        "- `decision_parity_diff.csv`: 连续流与 fold 重放之间的决策差异。",
        "- `max_drawdown_basis_audit.md`: 回撤计算基准专项审计。",
        "- `run_meta.json`: 代码、配置、数据和 fold 元信息。",
    ]
    (output_dir / "risk_state_policy_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_drawdown_audit(
    *,
    output_dir: Path,
    fold_rows: list[dict[str, Any]],
    execution: pd.DataFrame,
    initial_capital: float,
    contract_multiplier: float,
    threshold: float | None,
    parity: dict[str, Any],
) -> None:
    residual_max: float | None = None
    account_min: float | None = None
    account_max: float | None = None
    peak_min: float | None = None
    peak_max: float | None = None
    if not execution.empty:
        account = pd.to_numeric(execution.get("current_account_equity"), errors="coerce")
        points = pd.to_numeric(execution.get("current_equity_points"), errors="coerce")
        peak = pd.to_numeric(execution.get("risk_peak_equity"), errors="coerce")
        residual = (account - float(initial_capital) - points * float(contract_multiplier)).abs()
        residual_max = _series_float(residual, "max")
        account_min = _series_float(account, "min")
        account_max = _series_float(account, "max")
        peak_min = _series_float(peak, "min")
        peak_max = _series_float(peak, "max")
    drawdowns = [
        float(row["max_account_drawdown_pct"])
        for row in fold_rows
        if row.get("max_account_drawdown_pct") is not None
        and not pd.isna(row.get("max_account_drawdown_pct"))
    ]
    max_drawdown = max(drawdowns) if drawdowns else None
    rejection_count = sum(
        _reason_count(str(row.get("risk_rejection_reasons", "{}")), "max_drawdown")
        for row in fold_rows
    )
    lines = [
        "# max_drawdown_pct 计算基准审计",
        "",
        "## 结论",
        "",
        "- 原回测把累计盈亏点数直接作为 current_equity，与百分比阈值混用，属于单位错误。",
        f"- 修正后的口径：{RISK_DRAWDOWN_BASIS}",
        f"- 配置阈值：{_format_pct(threshold)}；本次 fold-reset 在信号审批时的 drawdown 拒绝数：{rejection_count}。",
        "",
        "## 产物验证",
        "",
        f"- 初始资金：{float(initial_capital):.2f}；合约乘数：{float(contract_multiplier):.4g}。",
        f"- 账户权益换算最大残差：{_format_number(residual_max)}。",
        f"- 信号时账户权益范围：{_format_number(account_min)} -> {_format_number(account_max)}。",
        f"- 风控峰值权益范围：{_format_number(peak_min)} -> {_format_number(peak_max)}。",
        f"- 各 fold 逐 bar 最大账户回撤的最大值：{_format_pct(max_drawdown)}。",
        "",
        "## 报告口径",
        "",
        "- 正式研究报告采用双口径并列：连续状态为主口径，fold-reset 为状态敏感性诊断。",
        f"- 决策流同一性：{_parity_note(parity).removeprefix('- ')}",
    ]
    (output_dir / "max_drawdown_basis_audit.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def write_meta(
    *,
    output_dir: Path,
    config_path: Path,
    current: pd.DataFrame,
    parent: pd.DataFrame,
    child: pd.DataFrame,
    continuous_source: Path,
    folds: list[dict[str, Any]],
    parity: dict[str, Any],
) -> None:
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(PROJECT_ROOT),
        "output_dir": str(output_dir),
        "git_head": _git_output("rev-parse", "HEAD"),
        "git_branch": _git_output("branch", "--show-current"),
        "git_status_short": _git_output("status", "--porcelain"),
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "continuous_source": str(continuous_source),
        "frame_ranges": {
            "current_start": str(current["datetime"].min()),
            "current_end": str(current["datetime"].max()),
            "current_rows": int(len(current)),
            "parent_rows": int(len(parent)),
            "child_rows": int(len(child)),
        },
        "folds": [
            {key: str(value) for key, value in fold.items()}
            for fold in folds
        ],
        "no_tuning_notice": NO_TUNING_NOTICE,
        "risk_drawdown_basis": RISK_DRAWDOWN_BASIS,
        "decision_parity": parity,
    }
    (output_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resolve_continuous_source(source: Path | None, output_dir: Path) -> Path:
    if source is not None:
        path = source if source.is_absolute() else PROJECT_ROOT / source
        if not path.exists():
            raise FileNotFoundError(f"continuous source not found: {path}")
        return path
    candidates = [
        PROJECT_ROOT / "reports" / "rule_mechanism_audit" / "20260810_full_current_head_exec_audit" / "source",
        PROJECT_ROOT / "reports" / "rule_mechanism_audit" / "20260810_full_current_head_exec_audit_v2" / "source",
    ]
    for path in candidates:
        if (path / "execution_decisions.csv").exists():
            return path
    raise FileNotFoundError(
        "continuous source with execution_decisions.csv not found; pass --continuous-source-dir"
    )


def _filter_windows(frame: pd.DataFrame, time_col: str, folds: list[dict[str, Any]]) -> pd.DataFrame:
    if frame.empty or time_col not in frame.columns or not folds:
        return pd.DataFrame(columns=frame.columns)
    times = pd.to_datetime(frame[time_col])
    mask = pd.Series(False, index=frame.index)
    for fold in folds:
        mask |= (times >= pd.Timestamp(fold["test_start"])) & (times < pd.Timestamp(fold["test_end"]))
    return frame[mask].reset_index(drop=True)


def _comparison_line(label: str, row: dict[str, Any]) -> str:
    return (
        f"- {label}: trades={row.get('trade_count', 0)}, net={row.get('net_pnl_points', 0)} pts, "
        f"accepted={row.get('accepted_count', 0)}, accepted_not_executed={row.get('accepted_not_executed_count', 0)}, "
        f"risk_rejected={row.get('risk_rejected_count', 0)}, reasons={row.get('risk_rejection_reasons', '{}')}"
    )


def _delta_line(label: str, left: dict[str, Any], right: dict[str, Any], key: str) -> str:
    left_value = float(left.get(key, 0) or 0)
    right_value = float(right.get(key, 0) or 0)
    return f"- {label}: continuous={left_value:g}, fold_reset={right_value:g}, delta={right_value - left_value:g}"


def _fold_table_lines(fold_rows: list[dict[str, Any]]) -> list[str]:
    if not fold_rows:
        return ["- No fold-reset rows."]
    lines = [
        "| Fold | Test Window | Accepted | Trades | Net Pts | Max Account DD | Risk Rejected | Top Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in fold_rows:
        top_reason = _top_reason(str(row.get("risk_rejection_reasons", "{}")))
        lines.append(
            f"| {int(row['fold']):02d} | {row['test_start']} -> {row['test_end']} | "
            f"{int(row['accepted_count'])} | {int(row['trade_count'])} | "
            f"{float(row['net_pnl_points']):.1f} | {_format_pct(row.get('max_account_drawdown_pct'))} | "
            f"{int(row['risk_rejected_count'])} | {top_reason} |"
        )
    return lines


def _top_reason(value: str) -> str:
    try:
        counts = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not counts:
        return ""
    reason, count = max(counts.items(), key=lambda item: item[1])
    return f"{reason} ({count})"


def _drawdown_note(fold_rows: list[dict[str, Any]]) -> str:
    reasons = " ".join(str(row.get("risk_rejection_reasons", "")) for row in fold_rows)
    if "max_drawdown" not in reasons:
        return (
            "- Fold-reset 口径未触发 `max_drawdown_pct`。"
            f"计算基准已确认为：{RISK_DRAWDOWN_BASIS}"
        )
    return (
        "- Fold-reset 口径触发了 `max_drawdown_pct`。"
        f"计算基准已确认为：{RISK_DRAWDOWN_BASIS}"
    )


def _parity_note(parity: dict[str, Any]) -> str:
    if parity.get("exact"):
        return "- 决策流完全一致，可将成交差异归因于风控状态口径。"
    return (
        "- 决策流不完全一致："
        f"shared={parity.get('shared_count', 0)}, "
        f"continuous_only={parity.get('continuous_only_count', 0)}, "
        f"fold_reset_only={parity.get('fold_reset_only_count', 0)}, "
        f"accepted_mismatch={parity.get('accepted_mismatch_count', 0)}, "
        f"accepted_continuous_only={parity.get('accepted_continuous_only_count', 0)}。"
        "因此 fold-reset 结果是状态敏感性诊断，不是纯风险重置的严格因果估计。"
    )


def _format_pct(value: object) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


def _format_number(value: object) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return ""


def _series_float(series: pd.Series, operation: str) -> float | None:
    values = series.dropna()
    if values.empty:
        return None
    return float(getattr(values, operation)())


def _reason_count(value: str, prefix: str) -> int:
    try:
        counts = json.loads(value)
    except json.JSONDecodeError:
        return 0
    return sum(int(count) for reason, count in counts.items() if str(reason).startswith(prefix))


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [frame for frame in frames if not frame.empty]
    return pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _id_set(frame: pd.DataFrame, column: str) -> set[str]:
    if frame.empty or column not in frame.columns:
        return set()
    return {str(value) for value in frame[column].dropna() if str(value).strip()}


def _count_values(frame: pd.DataFrame, column: str, value: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int((frame[column].astype(str) == value).sum())


def _value_counts_json(frame: pd.DataFrame, column: str) -> str:
    if frame.empty or column not in frame.columns:
        return "{}"
    counts = frame[column].replace("", pd.NA).dropna().astype(str).value_counts().to_dict()
    return json.dumps(counts, ensure_ascii=False, sort_keys=True)


def _min_time(frame: pd.DataFrame, column: str) -> str:
    if frame.empty or column not in frame.columns:
        return ""
    return str(pd.to_datetime(frame[column]).min())


def _max_time(frame: pd.DataFrame, column: str) -> str:
    if frame.empty or column not in frame.columns:
        return ""
    return str(pd.to_datetime(frame[column]).max())


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


if __name__ == "__main__":
    raise SystemExit(main())
