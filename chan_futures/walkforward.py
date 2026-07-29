"""Walkforward 分析引擎 —— Anchored 训练/测试分割 + 滚动窗口。

WalkforwardRunner:
  1. 按日期切分数据为 train/test fold
  2. 每个 fold: train 期训练参数、test 期验证
  3. 输出 fold-by-fold 指标汇总
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import StrategyConfig
from .backtest import run_backtest
from strategy_policy.reporting import (
    compute_standard_metrics,
    save_standard_report,
    save_comparison_report,
)


@dataclass
class WalkforwardConfig:
    """Walkforward 参数。"""

    train_months: int = 60
    test_months: int = 12
    step_months: int | None = None      # None → 等于 test_months


class WalkforwardRunner:
    """Anchored walkforward 分析。

    用法:
        runner = WalkforwardRunner(
            base_config=config,
            train_months=60,
            test_months=12,
            step_months=12,
        )
        runner.run(output_dir=Path("reports/walkforward"))
    """

    def __init__(
        self,
        base_config: StrategyConfig,
        train_months: int = 60,
        test_months: int = 12,
        step_months: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.base_config = base_config
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months or test_months
        self.limit = limit

    def run(self, output_dir: Path) -> pd.DataFrame:
        """运行 walkforward 并保存报告。

        Returns:
            fold-by-fold metrics DataFrame。
        """
        # 加载全量数据
        bars = pd.read_parquet(self.base_config.data_path)
        if self.limit is not None:
            bars = bars.iloc[:self.limit]
        bars["datetime"] = pd.to_datetime(bars["datetime"])
        min_dt = bars["datetime"].min()
        max_dt = bars["datetime"].max()

        print(f"\nData: {min_dt.date()} → {max_dt.date()} ({len(bars):,} bars)")
        print(f"Window: {self.train_months}m train / {self.test_months}m test / {self.step_months}m step")

        output_dir.mkdir(parents=True, exist_ok=True)

        # 生成 fold 分割
        folds = self._generate_folds(min_dt, max_dt)
        print(f"Folds: {len(folds)}")

        all_metrics: list[tuple[str, object]] = []

        for fi, fold in enumerate(folds):
            train_bars = bars[
                (bars["datetime"] >= fold.train_start) &
                (bars["datetime"] < fold.train_end)
            ]
            test_bars = bars[
                (bars["datetime"] >= fold.test_start) &
                (bars["datetime"] < fold.test_end)
            ]

            if len(train_bars) < 100 or len(test_bars) < 100:
                print(f"  Fold {fi+1} ({fold.label}): skip (too few bars)")
                continue

            # ── Train (in-sample) ──
            t0 = time.time()
            train_result = run_backtest(self.base_config, frame=train_bars)
            train_elapsed = time.time() - t0

            train_metrics = compute_standard_metrics(
                train_result.bars, train_result.fills,
                train_result.trades, train_result.exit_events,
                strategy_name="wf_train", variant=fold.label,
                timeframe_minutes=15,
            )

            # ── Test (out-of-sample) ──
            t0 = time.time()
            test_result = run_backtest(self.base_config, frame=test_bars)
            test_elapsed = time.time() - t0

            test_metrics = compute_standard_metrics(
                test_result.bars, test_result.fills,
                test_result.trades, test_result.exit_events,
                strategy_name="wf_test", variant=fold.label,
                timeframe_minutes=15,
            )

            # ── Save & log ──
            fold_dir = output_dir / f"fold_{fi+1:02d}_{fold.label}"
            save_standard_report(
                fold_dir / "train",
                bars=train_result.bars, fills=train_result.fills,
                trades=train_result.trades, exit_events=train_result.exit_events,
                config={
                    "fold": fold.label, "period": "train",
                    "start": str(fold.train_start.date()),
                    "end": str(fold.train_end.date()),
                    "bars": len(train_bars),
                },
                metrics=train_metrics,
            )
            save_standard_report(
                fold_dir / "test",
                bars=test_result.bars, fills=test_result.fills,
                trades=test_result.trades, exit_events=test_result.exit_events,
                config={
                    "fold": fold.label, "period": "test",
                    "start": str(fold.test_start.date()),
                    "end": str(fold.test_end.date()),
                    "bars": len(test_bars),
                },
                metrics=test_metrics,
            )

            print(f"  Fold {fi+1} ({fold.label}): "
                  f"Train={train_metrics.total_return_points:,.0f}pts ({train_metrics.trade_count}t) | "
                  f"Test={test_metrics.total_return_points:,.0f}pts ({test_metrics.trade_count}t) | "
                  f"{train_elapsed + test_elapsed:.0f}s")

            all_metrics.append((f"{fold.label}_train", train_metrics))
            all_metrics.append((f"{fold.label}_test", test_metrics))

        # ── Comparison across folds ──
        comp_df = save_comparison_report(output_dir, all_metrics, sort_by="fold")
        print(f"\nFold comparison saved to {output_dir}/comparison.csv")

        # ── OOS summary ──
        oos_items = [(l, m) for l, m in all_metrics if "_test" in l]
        if oos_items:
            oos_returns = [m.total_return_points for _, m in oos_items]
            oos_wins = sum(1 for r in oos_returns if r > 0)
            print(f"\nOOS Summary: {len(oos_items)} folds")
            print(f"  Profitable: {oos_wins}/{len(oos_items)} ({oos_wins / len(oos_items):.0%})")
            print(f"  Mean OOS return: {sum(oos_returns) / len(oos_returns):,.0f} pts")
            print(f"  Total OOS return: {sum(oos_returns):,.0f} pts")

        return comp_df

    # ── Fold generation ──

    def _generate_folds(self, min_dt: pd.Timestamp, max_dt: pd.Timestamp) -> list[_Fold]:
        """生成 anchored fold 列表。"""
        folds: list[_Fold] = []
        cursor = min_dt

        while cursor + pd.DateOffset(months=self.train_months + self.test_months) <= max_dt:
            train_start = cursor
            train_end = cursor + pd.DateOffset(months=self.train_months)
            test_start = train_end
            test_end = train_end + pd.DateOffset(months=self.test_months)

            label = f"{train_start.year}{train_start.month:02d}-{test_end.year}{test_end.month:02d}"
            folds.append(_Fold(
                label=label,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            ))

            cursor += pd.DateOffset(months=self.step_months)

        return folds


@dataclass
class _Fold:
    label: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
