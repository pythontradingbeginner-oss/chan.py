"""参数扫描引擎 —— 网格搜索 + 汇总对比。

SweepRunner:
  1. 解析 --param specs 为 Cartesian 积
  2. 每个组合调用 run_backtest()
  3. 汇总 comparison.csv (按得分排序)
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .config import StrategyConfig
from .backtest import run_backtest, BacktestResult
from strategy_policy.reporting import (
    compute_standard_metrics,
    save_standard_report,
    save_comparison_report,
)


@dataclass
class _ParamAxis:
    """一个扫描轴：路径 → 可选值列表。"""

    path: list[str]       # e.g. ["exits", "TrailingStopRule", "trigger_points"]
    values: list[Any]     # e.g. [300.0, 500.0, 800.0]


class SweepRunner:
    """参数网格扫描。

    用法:
        runner = SweepRunner(base_config=config, limit=5000)
        runner.run(
            param_specs=[
                "grading.min_grade=ideal,standard",
                "exits.TrailingStopRule.trigger_points=300,500,800",
            ],
            output_dir=Path("reports/sweep"),
        )
    """

    def __init__(self, base_config: StrategyConfig, limit: int | None = None) -> None:
        self.base_config = base_config
        self.limit = limit

    def run(
        self,
        param_specs: list[str],
        output_dir: Path,
    ) -> pd.DataFrame:
        """运行扫描并保存报告。

        Returns:
            comparison DataFrame (按 robust_score 降序排列)。
        """
        axes = [_parse_param_spec(s) for s in param_specs]
        grid = list(itertools.product(*[ax.values for ax in axes]))

        axis_labels = [".".join(ax.path) for ax in axes]
        print(f"\nSweep: {len(axes)} axes × {len(grid)} combos")
        for ax in axes:
            print(f"  {'.'.join(ax.path)}: {ax.values}")

        output_dir.mkdir(parents=True, exist_ok=True)
        all_metrics: list[tuple[str, object]] = []

        for combo_values in grid:
            label_parts = []
            cfg = self.base_config
            for ax, val in zip(axes, combo_values):
                label_parts.append(f"{ax.path[-1]}={_fmt(val)}")
                cfg = _apply_param(cfg, ax.path, val)
            label = "_".join(label_parts)

            t0 = time.time()
            result = run_backtest(cfg, limit=self.limit)
            elapsed = time.time() - t0

            metrics = compute_standard_metrics(
                result.bars, result.fills, result.trades, result.exit_events,
                strategy_name="sweep", variant=label, timeframe_minutes=15,
            )

            save_standard_report(
                output_dir / label,
                bars=result.bars, fills=result.fills, trades=result.trades,
                exit_events=result.exit_events,
                config={"label": label, **{ax.path[-1]: _fmt(v) for ax, v in zip(axes, combo_values)}},
                metrics=metrics,
            )

            pnls = [t.get("pnl_points", 0) for t in result.trades]
            print(f"  [{label:<40}] {elapsed:.0f}s | "
                  f"T={metrics.trade_count:>2} R={sum(pnls):>8,.0f}pts "
                  f"DD={metrics.max_drawdown_points:>6,.0f}pts "
                  f"WR={metrics.win_rate:.1%} PF={metrics.profit_factor:.2f}")

            all_metrics.append((label, metrics))

        comp_df = save_comparison_report(output_dir, all_metrics, sort_by="total_return_points")
        print(f"\nComparison saved to {output_dir}/comparison.csv")

        # Print top 5
        top = sorted(all_metrics, key=lambda x: -x[1].total_return_points)[:5]
        print("\nTop 5:")
        for i, (l, m) in enumerate(top):
            print(f"  {i+1}. {l}: R={m.total_return_points:,.0f}pts "
                  f"DD={m.max_drawdown_points:,.0f} WR={m.win_rate:.1%} PF={m.profit_factor:.2f}")

        return comp_df


# ═══════════════════════════════════════════
# 参数解析
# ═══════════════════════════════════════════


def _parse_param_spec(spec: str) -> _ParamAxis:
    """解析 'path.to.key=val1,val2,val3' 格式。"""
    if "=" not in spec:
        raise ValueError(f"参数格式错误 (缺 '='): {spec}。正确格式: key=val1,val2,val3")
    path_str, values_str = spec.split("=", 1)
    path = path_str.split(".")
    values_raw = values_str.split(",")
    values = [_coerce_value(v.strip()) for v in values_raw]
    return _ParamAxis(path=path, values=values)


def _coerce_value(raw: str) -> Any:
    """自动推断类型：int → float → str。"""
    raw = raw.strip()
    # int
    try:
        return int(raw)
    except ValueError:
        pass
    # float
    try:
        return float(raw)
    except ValueError:
        pass
    # bool
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    # str
    return raw


# ═══════════════════════════════════════════
# 配置参数应用
# ═══════════════════════════════════════════


def _replace_nested(cfg: StrategyConfig, attr: str, new_value: object) -> StrategyConfig:
    """替换 StrategyConfig 的嵌套属性。"""
    return StrategyConfig(
        code=cfg.code, kl_type=cfg.kl_type, data_path=cfg.data_path,
        allow_short=cfg.allow_short,
        chan=cfg.chan if attr != "chan" else new_value,
        grading=cfg.grading if attr != "grading" else new_value,
        entry=cfg.entry,
        exits=cfg.exits,
        risk=cfg.risk if attr != "risk" else new_value,
        sizing=cfg.sizing if attr != "sizing" else new_value,
        execution=cfg.execution if attr != "execution" else new_value,
    )


def _apply_param(cfg: StrategyConfig, path: list[str], value: Any) -> StrategyConfig:
    """将参数值应用到配置树的指定路径。返回新 StrategyConfig。

    支持路径:
      grading.min_grade=ideal,standard
      exits.StructureStopRule.grade_tighten=true,false
      exits.TrailingStopRule.trigger_points=300,500
      execution.fee_points=1.0,2.0
    """
    # 第一段决定是顶层的哪个子配置
    segment = path[0]

    if segment == "grading":
        from .config import GradingParams
        from dataclasses import replace
        return _replace_nested(cfg, "grading",
                               replace(cfg.grading, **{path[1]: value}))

    elif segment == "execution":
        from .config import ExecutionParams
        from dataclasses import replace
        return _replace_nested(cfg, "execution",
                               replace(cfg.execution, **{path[1]: value}))

    elif segment == "risk":
        from .config import RiskParams
        from dataclasses import replace
        return _replace_nested(cfg, "risk",
                               replace(cfg.risk, **{path[1]: value}))

    elif segment == "sizing":
        from .config import SizingParams
        from dataclasses import replace
        return _replace_nested(cfg, "sizing",
                               replace(cfg.sizing, **{path[1]: value}))

    elif segment == "chan":
        from .config import ChanParams
        from dataclasses import replace
        return _replace_nested(cfg, "chan",
                               replace(cfg.chan, **{path[1]: value}))

    elif segment == "exits":
        # path: exits.RuleType.param_key
        if len(path) < 3:
            raise ValueError(f"exits path needs 3 segments: exits.RuleType.param_key, got {path}")
        rule_type = path[1]
        param_key = path[2]
        return _apply_to_exit_rules(cfg, rule_type, param_key, value)

    else:
        raise ValueError(f"未知配置段: {segment}，可用: grading/execution/risk/sizing/chan/exits")


def _apply_to_exit_rules(cfg: StrategyConfig, rule_type: str, key: str, value: Any) -> StrategyConfig:
    """修改退出规则参数。"""
    new_exits = []
    for spec in cfg.exits:
        if spec.type == rule_type:
            new_params = dict(spec.params)
            new_params[key] = value
            from .config import ExitRuleSpec
            new_exits.append(ExitRuleSpec(type=spec.type, priority=spec.priority, params=new_params))
        else:
            new_exits.append(spec)
    return StrategyConfig(
        code=cfg.code, kl_type=cfg.kl_type, data_path=cfg.data_path,
        allow_short=cfg.allow_short,
        chan=cfg.chan, grading=cfg.grading, entry=cfg.entry,
        exits=new_exits, risk=cfg.risk, sizing=cfg.sizing,
        execution=cfg.execution,
    )


def _rebuild_config(cfg: StrategyConfig, path: list[str], new_leaf: object) -> StrategyConfig:
    """沿路径重建 StrategyConfig。"""
    from dataclasses import replace

    if len(path) == 0:
        return cfg

    segment = path[0]
    if segment == "grading":
        return replace(cfg, grading=new_leaf)
    elif segment == "execution":
        return replace(cfg, execution=new_leaf)
    elif segment == "risk":
        return replace(cfg, risk=new_leaf)
    elif segment == "sizing":
        return replace(cfg, sizing=new_leaf)
    elif segment == "chan":
        return replace(cfg, chan=new_leaf)
    else:
        return cfg


def _fmt(val: Any) -> str:
    """格式化值用于标签。"""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).replace(" ", "_")
