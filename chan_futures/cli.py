"""CLI 工具 —— chan 命令行入口。

用法:
    chan backtest --config configs/rb_15m_trend_ideal.yaml --output reports/my_test
    chan sweep --config configs/rb_15m_trend_ideal.yaml --param exits.TrailingStopRule.trigger_points=300,500,800
    chan walkforward --config configs/rb_15m_trend_ideal.yaml --train-months 60 --test-months 12
    chan data-check --product RB --timeframe 15m
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from .config import StrategyConfig
from .config_loader import load_config
from .backtest import run_backtest, BacktestResult
from .sweep import SweepRunner
from .walkforward import WalkforwardRunner


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════


def _print_result(result: BacktestResult, elapsed: float) -> None:
    """打印回测结果摘要。"""
    pnls = [t.get("pnl_points", 0) for t in result.trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    curve = result.bars["equity_points"].astype(float)
    total_ret = float(curve.iloc[-1])
    max_dd = float((curve.cummax() - curve).max())

    click.echo(f"  Bars:     {len(result.bars):,}")
    click.echo(f"  Trades:   {len(result.trades)}")
    click.echo(f"  PnL:      {total_ret:,.0f} pts")
    click.echo(f"  Max DD:   {max_dd:,.0f} pts")
    click.echo(f"  MAR:      {abs(total_ret / max_dd):.2f}" if max_dd != 0 else "  MAR:      inf")
    click.echo(f"  Win rate: {len(wins) / max(len(pnls), 1):.1%}")
    click.echo(f"  PF:       {sum(wins) / abs(sum(losses)):.2f}" if losses else "  PF:       inf")
    click.echo(f"  Elapsed:  {elapsed:.0f}s")

    errors = result.exit_manager.error_summary()
    if errors:
        click.echo(f"  Rule errors: {errors}")


def _resolve_output(config_path: str, output: str | None) -> Path:
    """解析输出目录。"""
    if output:
        return Path(output)
    # 默认：config 文件名作为输出目录名
    stem = Path(config_path).stem
    return Path(f"reports/{stem}")


# ════════════════════════════════════════════════════════════════
# CLI group
# ════════════════════════════════════════════════════════════════


@click.group()
@click.version_option(version="0.1.0", prog_name="chan")
def main() -> None:
    """chan.py 量化策略 CLI —— 回测、参数扫描、Walkforward 分析。"""


# ════════════════════════════════════════════════════════════════
# backtest
# ════════════════════════════════════════════════════════════════


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="策略配置文件 (.yaml / .json)")
@click.option("--output", "-o", default=None, type=click.Path(),
              help="输出目录 (默认: reports/<config_name>)")
@click.option("--limit", "-n", default=None, type=int,
              help="限制回测 bar 数 (用于快速调试)")
def backtest(config: str, output: str | None, limit: int | None) -> None:
    """运行单次回测。"""
    cfg = load_config(config)
    output_dir = _resolve_output(config, output)

    click.echo(f"Config:  {cfg.code} {cfg.kl_type} grade={cfg.grading.min_grade.value}")
    click.echo(f"Exits:   {[e.type for e in cfg.exits]}")
    click.echo(f"Output:  {output_dir}")

    t0 = time.time()
    result = run_backtest(cfg, limit=limit)
    elapsed = time.time() - t0

    _print_result(result, elapsed)

    result.save(output_dir)
    click.echo(f"\nReports saved to {output_dir}/")


# ════════════════════════════════════════════════════════════════
# sweep
# ════════════════════════════════════════════════════════════════


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="基础策略配置文件")
@click.option("--param", "-p", multiple=True,
              help="扫描参数 (可多次使用)。格式: key=val1,val2,val3。"
                   "例如: --param exits.TrailingStopRule.trigger_points=300,500,800")
@click.option("--output", "-o", default=None, type=click.Path(),
              help="输出目录 (默认: reports/sweep)")
@click.option("--limit", "-n", default=None, type=int,
              help="限制每个组合的 bar 数")
def sweep(config: str, param: tuple[str, ...], output: str | None, limit: int | None) -> None:
    """参数网格扫描。

    示例:
        chan sweep -c configs/rb_15m_trend_ideal.yaml \\
          -p grading.min_grade=ideal,standard \\
          -p exits.TrailingStopRule.trigger_points=300,500,800
    """
    if not param:
        raise click.UsageError("至少需要一个 --param。使用 --help 查看格式。")

    cfg = load_config(config)
    output_dir = Path(output) if output else Path("reports/sweep")

    click.echo(f"Base config: {cfg.code} {cfg.kl_type}")
    click.echo(f"Params: {list(param)}")
    click.echo(f"Output: {output_dir}")

    runner = SweepRunner(base_config=cfg, limit=limit)
    runner.run(param_specs=list(param), output_dir=output_dir)


# ════════════════════════════════════════════════════════════════
# walkforward
# ════════════════════════════════════════════════════════════════


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True),
              help="策略配置文件")
@click.option("--train-months", default=60, type=int,
              help="训练窗口月数 (默认: 60)")
@click.option("--test-months", default=12, type=int,
              help="测试窗口月数 (默认: 12)")
@click.option("--step-months", default=None, type=int,
              help="滚动步长月数 (默认: 等于 test-months)")
@click.option("--output", "-o", default=None, type=click.Path(),
              help="输出目录 (默认: reports/walkforward)")
@click.option("--limit", "-n", default=None, type=int,
              help="限制每个 fold 的 bar 数")
def walkforward(
    config: str,
    train_months: int,
    test_months: int,
    step_months: int | None,
    output: str | None,
    limit: int | None,
) -> None:
    """Walkforward 渐进式分析。

    前 N 个月训练，后 M 个月测试，窗口滚动推进。
    """
    cfg = load_config(config)
    output_dir = Path(output) if output else Path("reports/walkforward")
    step = step_months or test_months

    click.echo(f"Config:     {cfg.code} {cfg.kl_type}")
    click.echo(f"Train:      {train_months} months")
    click.echo(f"Test:       {test_months} months")
    click.echo(f"Step:       {step} months")
    click.echo(f"Output:     {output_dir}")

    runner = WalkforwardRunner(
        base_config=cfg,
        train_months=train_months,
        test_months=test_months,
        step_months=step,
        limit=limit,
    )
    runner.run(output_dir=output_dir)


# ════════════════════════════════════════════════════════════════
# data-check
# ════════════════════════════════════════════════════════════════


@main.command()
@click.option("--product", "-p", default="RB", help="品种代码 (默认: RB)")
@click.option("--timeframe", "-t", default="15m", help="K线周期 (默认: 15m)")
def data_check(product: str, timeframe: str) -> None:
    """检查数据可用性。"""
    import pandas as pd

    path = Path(f"data/processed/{product}_{timeframe}_continuous_raw.parquet")
    if not path.exists():
        click.echo(f"数据文件不存在: {path}")
        return

    df = pd.read_parquet(path)
    t_min = df["datetime"].min()
    t_max = df["datetime"].max()

    click.echo(f"Product:   {product}")
    click.echo(f"Timeframe: {timeframe}")
    click.echo(f"Bars:      {len(df):,}")
    click.echo(f"Range:     {t_min} → {t_max}")

    # Contract coverage
    if "active_symbol" in df.columns:
        contracts = df["active_symbol"].dropna().unique()
        click.echo(f"Contracts: {len(contracts)} ({', '.join(str(c) for c in contracts[:8])}{'...' if len(contracts) > 8 else ''})")


if __name__ == "__main__":
    main()
