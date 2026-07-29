"""Before/After 比较: 原始 MinimalChanTrendStrategy vs Grade-filtered 版本。

运行:
  python scripts/run_grade_filter_backtest_compare.py --limit 5000

输出:
  - reports/grade_filter_compare/before_bars.csv   (原始策略)
  - reports/grade_filter_compare/after_bars.csv    (grade-filtered)
  - reports/grade_filter_compare/comparison.csv    (对比汇总)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from chan_futures.backtest import ChanBacktestConfig
from chan_futures.execution import Fill, SimulatedExecutionEngine
from chan_futures.feed import prepare_ohlc_frame, row_to_klu
from chan_futures.graded_strategy import GradedChanStrategy, GradeFilterConfig
from chan_futures.risk import RiskConfig, RiskManager
from chan_futures.strategy import MinimalChanTrendStrategy
from Common.CEnum import KL_TYPE
from signal_core import SignalExtractor


def _run_once(
    bars: pd.DataFrame,
    config: ChanBacktestConfig,
    *,
    use_grade_filter: bool = False,
    min_grade: str = "standard",
) -> tuple[pd.DataFrame, list[Fill]]:
    chan = _new_chan(config)
    if use_grade_filter:
        strategy = GradedChanStrategy(
            GradeFilterConfig(
                accepted_bsp_types=None,
                allow_short=config.allow_short,
                min_grade=min_grade,
            )
        )
        extractor = SignalExtractor(
            symbol="RB",
            timeframe=config.kl_type.name.replace("K_", "").replace("M", "m"),
        )
    else:
        strategy = MinimalChanTrendStrategy(allow_short=config.allow_short)
        extractor = None

    risk = RiskManager(
        RiskConfig(
            max_abs_position=config.max_abs_position,
            max_loss_points=config.max_loss_points,
        )
    )
    execution = SimulatedExecutionEngine(
        fee_points=config.fee_points,
        slippage_points=config.slippage_points,
    )

    records: list[dict] = []
    for _, row in bars.iterrows():
        klu = row_to_klu(row, kl_type=config.kl_type)
        chan.trigger_load({config.kl_type: [klu]})
        price = float(row["close"])
        timestamp = row["datetime"]

        if use_grade_filter:
            graded = strategy.on_bar(
                chan=chan,
                current_position=execution.state.position,
                price=price,
                timestamp=timestamp,
                extractor=extractor,
            )
            signal = graded.signal if graded is not None else None
        else:
            signal = strategy.on_bar(
                chan=chan,
                current_position=execution.state.position,
                price=price,
                timestamp=timestamp,
            )

        fill = None
        risk_reason = None
        if signal is not None:
            decision = risk.approve(signal, realized_points=execution.state.realized_points)
            risk_reason = decision.reason
            if decision.approved:
                fill = execution.execute(signal)

        records.append({
            "datetime": timestamp,
            "close": price,
            "position": execution.state.position,
            "equity_points": execution.mark_to_market(price),
            "realized_points": execution.state.realized_points,
            "signal_action": signal.action if signal else None,
            "signal_bsp_type": signal.bsp_type if signal else None,
            "risk_reason": risk_reason,
            "fill_price": fill.fill_price if fill else None,
        })

    return pd.DataFrame(records), list(execution.fills)


def _new_chan(config):
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_SRC

    chan_cfg = {
        "trigger_step": True,
        "bi_strict": True,
        "divergence_rate": float("inf"),
        "bsp2_follow_1": False,
        "bsp3_follow_1": False,
        "min_zs_cnt": 0,
        "bs1_peak": False,
        "macd_algo": "peak",
        "bs_type": "1,1p,2,2s,3a,3b",
        "print_warning": False,
    }
    if config.chan_config:
        chan_cfg.update(config.chan_config)
    return CChan(
        code=config.code,
        begin_time=None, end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[config.kl_type],
        config=CChanConfig(chan_cfg),
        autype=AUTYPE.NONE,
    )


def _summary(equity: pd.DataFrame, fills: list[Fill], label: str) -> dict:
    if equity.empty:
        return {"label": label, "bar_count": 0, "fill_count": 0,
                "total_return": 0.0, "max_drawdown": 0.0, "win_rate": 0.0}
    curve = equity["equity_points"].astype(float)
    total_return = float(curve.iloc[-1])
    dd = float((curve.cummax() - curve).max())
    win_rate = 0.0
    if fills:
        realized = [f for f in fills if abs(f.quantity_delta) > 0]
        wins = sum(
            1 for i in range(1, len(realized), 2)
            if i < len(realized) and realized[i].equity_points > realized[i-1].equity_points
        )
        trades = len(realized) // 2
        win_rate = wins / max(trades, 1)
    return {
        "label": label,
        "bar_count": int(len(equity)),
        "fill_count": int(len(fills)),
        "total_return_points": round(total_return, 2),
        "max_drawdown_points": round(dd, 2),
        "win_rate": round(win_rate, 3),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Before/after grade filter backtest.")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--reports-dir", type=Path, default=Path("reports/grade_filter_compare"))
    p.add_argument("--fee-points", type=float, default=1.0)
    p.add_argument("--slippage-points", type=float, default=1.0)
    p.add_argument("--min-grade", type=str, default="standard",
                   choices=["ideal", "standard", "weak"])
    args = p.parse_args()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.read_parquet("data/processed/RB_15m_continuous_raw.parquet")
    frame = prepare_ohlc_frame(frame)
    limit = None if args.limit == 0 else args.limit
    if limit:
        frame = frame.iloc[:limit].reset_index(drop=True)

    print(f"Running comparison on {len(frame)} bars | min_grade={args.min_grade}")

    # ── Run BOTH passes ──
    base_cfg = ChanBacktestConfig(
        kl_type=KL_TYPE.K_15M,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
        allow_short=True,
    )

    print("  [1/2] before (no grade filter) ...")
    before_eq, before_fills = _run_once(frame.copy(), base_cfg, use_grade_filter=False)
    print("  [2/2] after (grade filter, min=%s) ..." % args.min_grade)
    after_eq, after_fills = _run_once(frame.copy(), base_cfg, use_grade_filter=True,
                                       min_grade=args.min_grade)

    # ── Save raw outputs ──
    before_eq.to_csv(args.reports_dir / "before_bars.csv", index=False, encoding="utf-8-sig")
    after_eq.to_csv(args.reports_dir / "after_bars.csv", index=False, encoding="utf-8-sig")

    # ── Summary ──
    b = _summary(before_eq, before_fills, "before (no filter)")
    a = _summary(after_eq, after_fills, f"after (min_{args.min_grade})")
    comparison = pd.DataFrame([b, a])
    comparison.to_csv(args.reports_dir / "comparison.csv", index=False, encoding="utf-8-sig")

    print(f"\n{'='*60}")
    print("Backtest Comparison")
    print(f"{'='*60}")
    for row in [b, a]:
        print(f"  {row['label']}:")
        print(f"    fills={row['fill_count']}  return={row['total_return_points']} pts  "
              f"dd={row['max_drawdown_points']} pts  win_rate={row['win_rate']:.2%}")
    print(f"{'='*60}")
    print(f"saved to {args.reports_dir}")


if __name__ == "__main__":
    main()
