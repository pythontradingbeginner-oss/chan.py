"""Phase D: Walk-forward + cost stress testing — delegated to unified backtest core.

Depends on: chan_futures.backtest.run_backtest() and chan_futures.config.StrategyConfig.
"""

from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings("ignore")

import pandas as pd

from chan_futures.config import StrategyConfig, ExecutionParams, ScoreGradeStr
from chan_futures.config_loader import load_config
from chan_futures.backtest import run_backtest
from chan_futures.feed import prepare_ohlc_frame


def main() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "rb_15m_trend_ideal.yaml")
    frame = pd.read_parquet(config.data_path)
    print(f"Data: {len(frame)} bars", flush=True)

    out = Path("reports/walkforward")
    out.mkdir(parents=True, exist_ok=True)

    # ══════════════════════════════════════════════════════════════
    # D.1: Year-by-year segmentation
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print(f"D.1: Year-by-year Performance (grade={config.grading.min_grade.value})")
    print("=" * 60)

    bars = prepare_ohlc_frame(frame)
    min_dt = pd.Timestamp(bars["datetime"].min())
    max_dt = pd.Timestamp(bars["datetime"].max())
    print(f"Full range: {min_dt} -> {max_dt}")

    yearly: list[tuple[str, dict]] = []
    for year in range(2018, 2026):
        start = pd.Timestamp(f"{year}-01-01", tz=min_dt.tz)
        end = pd.Timestamp(f"{year+1}-01-01", tz=min_dt.tz)
        yb = bars[(bars["datetime"] >= start) & (bars["datetime"] < end)].reset_index(drop=True)
        if len(yb) < 100:
            print(f"  {year}: {len(yb)} bars -- skip")
            continue

        t0 = time.time()
        result = run_backtest(config, limit=len(yb))
        elapsed = time.time() - t0

        pnls = [t.get("pnl_points", 0) for t in result.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls) if pnls else 0
        pf = sum(wins) / abs(sum(losses)) if losses else float("inf")

        curve = result.bars["equity_points"].astype(float)
        total_ret = float(curve.iloc[-1]) if not curve.empty else 0.0
        max_dd = float((curve.cummax() - curve).max()) if not curve.empty else 0.0

        r = {
            "bar_count": len(result.bars),
            "trade_count": len(result.trades),
            "total_return": round(total_ret, 1),
            "max_drawdown": round(max_dd, 1),
            "mar_ratio": round(abs(total_ret / max_dd), 2) if max_dd != 0 else 0,
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 2),
            "avg_hold_bars": round(
                sum(t.get("hold_bars", 0) for t in result.trades) / max(len(result.trades), 1), 1),
        }
        yearly.append((str(year), r))
        print(f"  {year}: {r['trade_count']:>2} trades "
              f"Return={r['total_return']:>8,.0f}pts DD={r['max_drawdown']:>6,.0f}pts "
              f"WR={r['win_rate']:.0%} PF={r['profit_factor']:.2f} "
              f"{elapsed:.0f}s", flush=True)

    yr_rows = [{"year": y, **r} for y, r in yearly]
    pd.DataFrame(yr_rows).to_csv(out / "yearly.csv", index=False, encoding="utf-8-sig")

    # ══════════════════════════════════════════════════════════════
    # D.2: Main-contract segmentation
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print(f"D.2: Main-contract Segment Performance (grade={config.grading.min_grade.value})")
    print("=" * 60)

    if "active_symbol" in bars.columns:
        contracts = bars["active_symbol"].dropna().unique()
        contract_result: list[tuple[str, dict]] = []
        for c in contracts:
            cb = bars[bars["active_symbol"] == c].reset_index(drop=True)
            if len(cb) < 50:
                continue
            t0 = time.time()
            result = run_backtest(config, limit=len(cb))
            elapsed = time.time() - t0
            if len(result.trades) > 0:
                pnls = [t.get("pnl_points", 0) for t in result.trades]
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p <= 0]
                wr = len(wins) / len(pnls) if pnls else 0
                pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
                curve = result.bars["equity_points"].astype(float)
                total_ret = float(curve.iloc[-1]) if not curve.empty else 0.0
                max_dd = float((curve.cummax() - curve).max()) if not curve.empty else 0.0
                r = {
                    "bar_count": len(result.bars),
                    "trade_count": len(result.trades),
                    "total_return": round(total_ret, 1),
                    "max_drawdown": round(max_dd, 1),
                    "mar_ratio": round(abs(total_ret / max_dd), 2) if max_dd != 0 else 0,
                    "win_rate": round(wr, 4),
                    "profit_factor": round(pf, 2),
                }
                contract_result.append((str(c), r))
                print(f"  {c}: {r['trade_count']:>2} trades "
                      f"Return={r['total_return']:>8,.0f}pts DD={r['max_drawdown']:>6,.0f}pts "
                      f"WR={r['win_rate']:.0%} PF={r['profit_factor']:.2f} "
                      f"{elapsed:.0f}s", flush=True)

        c_rows = [{"contract": c, **r} for c, r in contract_result]
        pd.DataFrame(c_rows).to_csv(out / "contracts.csv", index=False, encoding="utf-8-sig")
    else:
        print("  No active_symbol column -- skipping")

    # ══════════════════════════════════════════════════════════════
    # D.3: Cost pressure testing (on 5000 bars for speed)
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("D.3: Cost Pressure Testing (5,000 bars)")
    print("=" * 60)

    b5000 = bars.iloc[:5000].reset_index(drop=True)
    cost_configs = [
        ("baseline", 1.0, 1.0),
        ("fee_2x", 2.0, 1.0),
        ("slip_2x", 1.0, 2.0),
        ("cost_2x", 2.0, 2.0),
        ("fee3_slip2", 3.0, 2.0),
        ("worst_case", 3.0, 3.0),
    ]

    cost_result: list[tuple[str, dict]] = []
    for label, fee, slip in cost_configs:
        t0 = time.time()
        cfg = StrategyConfig(
            code=config.code,
            kl_type=config.kl_type,
            data_path=config.data_path,
            grading=config.grading,
            exits=list(config.exits),
            execution=ExecutionParams(fee_points=fee, slippage_points=slip),
            risk=config.risk,
            sizing=config.sizing,
        )
        result = run_backtest(cfg, limit=5000)
        elapsed = time.time() - t0
        pnls = [t.get("pnl_points", 0) for t in result.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls) if pnls else 0
        pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
        curve = result.bars["equity_points"].astype(float)
        total_ret = float(curve.iloc[-1]) if not curve.empty else 0.0
        max_dd = float((curve.cummax() - curve).max()) if not curve.empty else 0.0
        r = {
            "bar_count": len(result.bars),
            "trade_count": len(result.trades),
            "total_return": round(total_ret, 1),
            "max_drawdown": round(max_dd, 1),
            "mar_ratio": round(abs(total_ret / max_dd), 2) if max_dd != 0 else 0,
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 2),
        }
        cost_result.append((label, r))
        print(f"  {label:<15} (fee={fee}, slip={slip}): "
              f"Return={r['total_return']:>8,.0f}pts DD={r['max_drawdown']:>6,.0f}pts "
              f"T={r['trade_count']:>2} PF={r['profit_factor']:.2f} "
              f"{elapsed:.0f}s", flush=True)

    pd.DataFrame([{"cost_scenario": c, **r} for c, r in cost_result]).to_csv(
        out / "cost_stress.csv", index=False, encoding="utf-8-sig")

    # ══════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print("Phase D Complete. Results saved to reports/walkforward/")
    print("=" * 60)

    print("\nYear-by-Year Summary:")
    for y, r in yearly:
        status = "+" if r["total_return"] > 0 else "-"
        print(f"  {y}: {status} {r['total_return']:>8,.0f}pts "
              f"(DD={r['max_drawdown']:>6,.0f}, WR={r['win_rate']:.0%}, "
              f"Trades={r['trade_count']})")

    profitable = sum(1 for _, r in yearly if r["total_return"] > 0)
    total_yr = len(yearly)
    print(f"\n  Profitable years: {profitable}/{total_yr} "
          f"({profitable/total_yr*100:.0f}%)" if total_yr else "")


if __name__ == "__main__":
    main()
