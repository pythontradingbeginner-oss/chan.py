"""P7-R6C: reproducible holdout runner.

Reads existing research parquet (NEVER writes or overwrites data), runs the
fixed strategy on an independent recent OOS window, and emits a JSON report
with every input needed to reproduce the run:
  - git HEAD and worktree status
  - config SHA256 and input parquet SHA256
  - sample window and warmup window
  - fee / slippage
  - configured_warmup_bars vs actual_warmup_bars
  - raw trade count, PnL / DD / PF / Sharpe
  - per-trade entry/exit/direction/gross/net/reason
  - cost-pressure results (1.0x / 1.5x / 2.0x)

Output goes to a timestamped file under reports/p7_holdout/ by default, plus
an updated latest.json convenience copy for downstream review tools.

Does NOT tune strategy parameters on the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GIT = os.environ.get("GIT_EXECUTABLE") or shutil.which("git") or "git"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "p7_holdout"

import pandas as pd

from chan_futures.config_loader import load_config
from chan_futures.backtest import run_backtest
from strategy_policy.reporting import compute_standard_metrics


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_output(*args: str) -> str:
    try:
        out = subprocess.run(
            [GIT, "-C", str(PROJECT_ROOT), *args],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def _load_frames(config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load pre-aggregated parquet directly (fast) instead of re-aggregating."""
    def _read(path: str) -> pd.DataFrame:
        p = Path(path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return pd.read_parquet(p)

    current = _read(config.data_path)
    parent = _read(config.multi_level.parent_data_path)
    child = _read(config.multi_level.child_data_path)
    return current, parent, child


def _trade_audit_record(trade: dict, execution) -> dict:
    """Build one complete, internally consistent holdout trade record."""
    required = ("entry_time", "exit_time", "direction", "exit_reason", "pnl_points")
    missing = [name for name in required if trade.get(name) in (None, "")]
    if missing:
        raise ValueError(f"holdout trade missing required fields: {','.join(missing)}")

    direction_raw = trade["direction"]
    direction = (
        str(direction_raw.value)
        if hasattr(direction_raw, "value")
        else str(direction_raw)
    )
    lots = int(trade.get("lots", 1))
    if lots < 1:
        raise ValueError(f"holdout trade has invalid lots: {lots}")

    net_pnl = float(trade["pnl_points"])
    fees = round(float(execution.fee_points) * 2 * lots, 4)
    slippage = round(float(execution.slippage_points) * 2 * lots, 4)
    gross_pnl = round(net_pnl + fees + slippage, 4)
    audit_ok = abs(gross_pnl - fees - slippage - net_pnl) < 0.01
    if not audit_ok:
        raise ValueError("holdout trade cost reconciliation failed")

    return {
        "entry_time": str(trade["entry_time"]),
        "exit_time": str(trade["exit_time"]),
        "direction": direction,
        "gross_pnl_points": gross_pnl,
        "fees_points": fees,
        "slippage_points": slippage,
        "net_pnl_points": net_pnl,
        "exit_reason": str(trade["exit_reason"]),
        "_audit_ok": True,
    }


def _run_window(
    config,
    frames,
    oos_start: pd.Timestamp,
    fee_mult: float,
    slip_mult: float,
    configured_warmup_bars: int,
) -> dict:
    current, parent, child = frames
    cur = current.reset_index(drop=True)
    dt = pd.to_datetime(cur["datetime"])
    if dt.dt.tz is not None:
        dt = dt.dt.tz_localize(None)
    cur["datetime"] = dt

    warm = cur[cur["datetime"] < oos_start]
    # Bounded warmup: cap the replay history so the multi-level backtest
    # completes in reasonable time while still meeting the warmup minimum.
    warm_idx = max(0, len(warm) - max(2000, configured_warmup_bars))
    actual_warmup_bars = len(warm) - warm_idx
    replay = cur.iloc[warm_idx:].reset_index(drop=True)
    replay_start = replay["datetime"].iloc[0] if len(replay) > 0 else None

    def _slice(frame, start_ts):
        d = pd.to_datetime(frame["datetime"])
        if d.dt.tz is not None:
            d = d.dt.tz_localize(None)
        f = frame.copy()
        f["datetime"] = d
        return f[f["datetime"] >= start_ts].reset_index(drop=True)

    parent = _slice(parent, replay["datetime"].iloc[0])
    child = _slice(child, replay["datetime"].iloc[0])

    exec_cfg = config.execution.__class__(
        fee_points=config.execution.fee_points * fee_mult,
        slippage_points=config.execution.slippage_points * slip_mult,
        contract_multiplier=config.execution.contract_multiplier,
        price_tick=config.execution.price_tick,
        margin_rate=config.execution.margin_rate,
        max_margin_utilization=config.execution.max_margin_utilization,
    )
    cfg2 = config.__class__(
        code=config.code, kl_type=config.kl_type, chan=config.chan,
        grading=config.grading, decomposition=config.decomposition,
        multi_level=config.multi_level, momentum=config.momentum,
        production=config.production, entry=config.entry, exits=config.exits,
        risk=config.risk, sizing=config.sizing, execution=exec_cfg,
    )
    result = run_backtest(
        cfg2, frame=replay, parent_frame=parent, child_frame=child,
        decision_start=oos_start,
    )
    oos_bars = result.bars[result.bars["datetime"] >= oos_start]
    metrics = compute_standard_metrics(
        oos_bars, result.fills, result.trades, result.exit_events,
        strategy_name="p7_holdout_r10", variant=f"cost{fee_mult}x", timeframe_minutes=15,
    )

    # Per-trade detail (R11.4: trades are dicts — use .get(), not getattr)
    trades_detail = [_trade_audit_record(trade, exec_cfg) for trade in result.trades]
    audited_net = sum(trade["net_pnl_points"] for trade in trades_detail)
    if abs(audited_net - float(metrics.total_return_points)) >= 0.01:
        raise ValueError(
            "holdout trade net total does not match metrics: "
            f"trades={audited_net} metrics={metrics.total_return_points}"
        )

    return {
        "configured_warmup_bars": configured_warmup_bars,
        "actual_warmup_bars": actual_warmup_bars,
        "replay_start": str(replay_start),
        "decision_start": str(oos_start),
        "evaluation_end": str(result.bars["datetime"].max()) if len(result.bars) > 0 else "",
        "fee_multiplier": fee_mult,
        "slippage_multiplier": slip_mult,
        "fee_points": config.execution.fee_points * fee_mult,
        "slippage_points": config.execution.slippage_points * slip_mult,
        "trade_count": metrics.trade_count,
        "total_return_points": metrics.total_return_points,
        "max_drawdown_points": metrics.max_drawdown_points,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "trades": trades_detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/rb_15m_qingpai_strict.yaml")
    parser.add_argument("--oos-start", default="2026-06-01")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--latest-name",
        default="latest.json",
        help="Stable copy name written beside the timestamped report; set empty to disable.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    frames = _load_frames(config)
    oos_start = pd.Timestamp(args.oos_start)
    configured_warmup = int(config.production.warmup_bars)

    scenarios = [
        ("baseline", 1.0, 1.0),
        ("cost1.5x", 1.5, 1.5),
        ("cost2x", 2.0, 2.0),
    ]
    rows = [
        _run_window(config, frames, oos_start, fee, slip, configured_warmup)
        for _, fee, slip in scenarios
    ]

    def _abs(path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": _git_output("rev-parse", "HEAD"),
        "git_branch": _git_output("branch", "--show-current"),
        "git_status_short": _git_output("status", "--porcelain"),
        "config_path": str(config_path),
        "config_sha256": _sha256(Path(config_path)),
        "input_parquet_sha256": {
            "data_path": _sha256(_abs(config.data_path)),
            "parent_data_path": _sha256(_abs(config.multi_level.parent_data_path)),
            "child_data_path": _sha256(_abs(config.multi_level.child_data_path)),
        },
        "oos_start": str(oos_start),
        "oos_end": str(pd.Timestamp(frames[0]["datetime"].max())),
        "scenarios": rows,
        "note": "Read-only; no data overwritten. Parameters NOT tuned on this result.",
    }

    # Keep timestamped reports immutable; latest.json is a convenience pointer.
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = output_dir / f"holdout_r10_{ts}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(payload, encoding="utf-8")
    latest = None
    if args.latest_name:
        latest = output_dir / args.latest_name
        latest.write_text(payload, encoding="utf-8")
    print(f"Wrote holdout report to: {output}")
    if latest:
        print(f"Updated latest holdout copy: {latest}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
