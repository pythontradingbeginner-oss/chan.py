"""标准化回测报告模块。

提供统一的报告格式和绩效指标计算, 供所有回测脚本使用。

V2 增强指标：
  - Sharpe: bar-level equity returns annualization, not trade-level PnL average
  - Sortino: proper downside deviation from zero benchmark
  - Calmar: return / max_dd (name fixed from "mar")
  - 盈亏比: avg_win / |avg_loss|
  - 成本分解: gross PnL vs net PnL, 费用滑点占比
  - 高峰回撤持续: drawdown duration in bars and hours
  - 收益分布: skewness, kurtosis of trade PnLs
  - 逐年明细: 每年 MAR/Sharpe/胜率/盈亏因子的独立计算
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════════════════
# 标准绩效指标
# ════════════════════════════════════════════════════════════════

@dataclass
class StandardMetrics:
    """所有回测策略的统一绩效指标 (V2 增强版)。"""

    # ── 标识 ──
    strategy_name: str = ""
    variant: str = ""
    computed_at: str = ""

    # ── 基础统计 ──
    bar_count: int = 0
    fill_count: int = 0
    trade_count: int = 0
    data_start: str = ""
    data_end: str = ""

    # ── 收益 ──
    total_return_points: float = 0.0       # 净利润（扣除费用/滑点后）
    max_drawdown_points: float = 0.0       # 最大回撤幅度（点）
    calmar_ratio: float = 0.0             # annualized return / max_dd
    total_return_pct: float = 0.0          # 总收益率 = 净利润 / 起始价 × 100
    annualized_return_pct: float = 0.0     # 年化收益率

    # ── 交易统计 ──
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win_points: float = 0.0
    avg_loss_points: float = 0.0
    avg_trade_points: float = 0.0
    payoff_ratio: float = 0.0              # avg_win / |avg_loss| (盈亏比)
    largest_win_points: float = 0.0
    largest_loss_points: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_hold_bars: float = 0.0
    avg_hold_hours: float = 0.0

    # ── 收益分布 ──
    std_trade_points: float = 0.0          # 逐笔 PnL 标准差
    skewness: float = 0.0                  # 正偏态 = 大赢小亏
    kurtosis: float = 0.0                  # 高峰态 = 极端值多
    expectancy: float = 0.0               # 每笔期望收益

    # ── 出场/入场级 MFE/MAE ──
    avg_mfe_points: float = 0.0            # 持仓期间平均最大浮盈
    avg_mae_points: float = 0.0            # 持仓期间平均最大浮亏
    avg_giveback_points: float = 0.0       # 平均回吐幅度

    # ── 风险调整 (bar-frequency equity curve) ──
    sharpe_ratio: float = 0.0              # 逐 bar 年化
    sortino_ratio: float = 0.0             # 下行偏差年化
    max_drawdown_duration_bars: int = 0    # 最大回撤持续 bar 数
    max_drawdown_duration_hours: float = 0.0

    # ── 成本分解 ──
    total_gross_points: float = 0.0        # 扣除费用前的总利润
    total_fee_points: float = 0.0          # 总手续费（点）
    total_slippage_points: float = 0.0     # 总滑点（点）
    cost_pct_of_gross: float = 0.0         # (fee + slippage) / gross × 100

    # ── 逐年收益 ──
    return_by_year: dict[str, float] = field(default_factory=dict)
    yearly_mar: dict[str, float] = field(default_factory=dict)
    yearly_sharpe: dict[str, float] = field(default_factory=dict)
    yearly_win_rate: dict[str, float] = field(default_factory=dict)
    yearly_profit_factor: dict[str, float] = field(default_factory=dict)
    yearly_trade_count: dict[str, int] = field(default_factory=dict)
    years_profitable: int = 0
    years_total: int = 0

    # ── 出场分析 ──
    exit_reason_breakdown: dict[str, int] = field(default_factory=dict)
    exit_rule_breakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("return_by_year", "yearly_mar", "yearly_sharpe",
                  "yearly_win_rate", "yearly_profit_factor", "yearly_trade_count",
                  "exit_reason_breakdown", "exit_rule_breakdown"):
            d[k] = json.dumps(d[k])
        return d

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([self.to_dict()])

    def print_summary(self) -> None:
        """Print a comprehensive risk report."""
        print(f"\n{'='*72}")
        print(f"  {self.strategy_name} / {self.variant}")
        print(f"{'='*72}")

        print(f"  {'Period:':<22} {self.data_start} → {self.data_end}")
        print(f"  {'Bars:':<22} {self.bar_count:,}")
        print(f"  {'Trades:':<22} {self.trade_count}")
        print(f"  {'Fills:':<22} {self.fill_count}")
        print()

        print(f"  {'── 收益 ──':<22}")
        print(f"  {'总净利润:':<22} {self.total_return_points:>10,.0f} pts")
        print(f"  {'总收益率:':<22} {self.total_return_pct:>10.2f}%")
        print(f"  {'年化收益率:':<22} {self.annualized_return_pct:>10.1f}%")
        print(f"  {'最大回撤 (Max DD):':<22} {self.max_drawdown_points:>10,.0f} pts")
        print(f"  {'Calmar Ratio:':<22} {self.calmar_ratio:>10.2f}")
        print()

        print(f"  {'── 交易统计 ──':<22}")
        print(f"  {'胜率 (Win Rate):':<22} {self.win_rate:>10.1%}")
        print(f"  {'盈亏比 (Payoff):':<22} {self.payoff_ratio:>10.2f}  (avg_win / |avg_loss|)")
        print(f"  {'盈利因子 (PF):':<22} {self.profit_factor:>10.2f}")
        print(f"  {'每笔期望 (Expectancy):':<22} {self.expectancy:>10.1f} pts")
        print(f"  {'平均盈利:':<22} {self.avg_win_points:>10.1f} pts")
        print(f"  {'平均亏损:':<22} {self.avg_loss_points:>10.1f} pts")
        print(f"  {'最大单笔盈利:':<22} {self.largest_win_points:>10,.0f} pts")
        print(f"  {'最大单笔亏损:':<22} {self.largest_loss_points:>10,.0f} pts")
        print(f"  {'最多连续盈利:':<22} {self.max_consecutive_wins:>10}")
        print(f"  {'最多连续亏损:':<22} {self.max_consecutive_losses:>10}")
        print(f"  {'平均持仓:':<22} {self.avg_hold_bars:>10.0f} bars ({self.avg_hold_hours:.1f}h)")
        print()

        print(f"  {'── 收益分布 ──':<22}")
        print(f"  {'收益标准差:':<22} {self.std_trade_points:>10.1f} pts")
        print(f"  {'偏态 (Skew):':<22} {self.skewness:>+10.2f}  {'(+ = 正偏/大赢小亏)' if self.skewness > 0 else '(- = 负偏/大亏小赢)'}")
        print(f"  {'峰态 (Kurt):':<22} {self.kurtosis:>10.2f}  {'(>0 = 肥尾/极端值多)' if self.kurtosis > 0 else '(<0 = 薄尾)'}")
        print(f"  {'平均 MFE:':<22} {self.avg_mfe_points:>10.1f} pts  (平均最大浮盈)")
        print(f"  {'平均 MAE:':<22} {self.avg_mae_points:>10.1f} pts  (平均最大浮亏)")
        print(f"  {'平均回吐:':<22} {self.avg_giveback_points:>10.1f} pts")
        print()

        print(f"  {'── 风险调整 ──':<22}")
        print(f"  {'年化 Sharpe:':<22} {self.sharpe_ratio:>10.2f}  (<1.0=差 1.0-2.0=中等 >2.0=好)")
        print(f"  {'年化 Sortino:':<22} {self.sortino_ratio:>10.2f}  (仅惩罚下行波动)")
        print(f"  {'最大回撤持续:':<22} {self.max_drawdown_duration_bars:>10} bars ({self.max_drawdown_duration_hours:.0f}h)")
        print()

        print(f"  {'── 成本分解 ──':<22}")
        print(f"  {'毛利 (不含成本):':<22} {self.total_gross_points:>10,.0f} pts")
        print(f"  {'总手续费:':<22} {self.total_fee_points:>10,.0f} pts")
        print(f"  {'总滑点:':<22} {self.total_slippage_points:>10,.0f} pts")
        print(f"  {'成本吞噬比:':<22} {self.cost_pct_of_gross:>10.1f}%  (成本/毛利)")
        print()

        # Bar-level Sharpe (already computed above as annualized via bars_per_year)
        # For the yearly print, show the per-year Sharpe from _compute_yearly_metrics
        if self.return_by_year:
            print(f"  {'── 逐年收益 ──':<22}")
            print(f"  {'':<6} {'交易数':>4}  {'净利润':>8}  {'Calmar':>7}  {'Sharpe':>7}  {'胜率':>7}  {'PF':>6}")
            print(f"  {'':<6} {'':>4}  {'(点)':>8}  {'':>7}  {'':>7}  {'':>7}  {'':>6}")
            for yr in sorted(self.return_by_year.keys()):
                yr_sharpe = self.yearly_sharpe.get(yr, 0)
                print(f"  {yr:<6} {self.yearly_trade_count.get(yr, 0):>4}  "
                      f"{self.return_by_year[yr]:>8,.0f}  "
                      f"{self.yearly_mar.get(yr, 0):>7.2f}  "
                      f"{yr_sharpe:>7.2f}  "
                      f"{self.yearly_win_rate.get(yr, 0):>7.1%}  "
                      f"{self.yearly_profit_factor.get(yr, 0):>6.2f}")
            print(f"  {'盈利年:':<6} {self.years_profitable}/{self.years_total} "
                  f"({self.years_profitable / max(self.years_total, 1):.0%})")

        if self.exit_reason_breakdown:
            print(f"\n  {'── 出场原因 ──':<22}")
            for reason, cnt in sorted(self.exit_reason_breakdown.items(), key=lambda x: -x[1]):
                pct = cnt / max(self.trade_count, 1) * 100
                print(f"  {reason:<20} {cnt:>4} ({pct:>5.1f}%)")

        print(f"{'='*72}")


# ════════════════════════════════════════════════════════════════
# 指标计算 (V2 增强版)
# ════════════════════════════════════════════════════════════════

# 假设全年有约 250 个交易日, 15m × 23 bars/day ≈ 5750 bars/year
BARS_PER_TRADING_DAY_15M = 23
TRADING_DAYS_PER_YEAR = 250
BARS_PER_YEAR_15M = BARS_PER_TRADING_DAY_15M * TRADING_DAYS_PER_YEAR


def compute_standard_metrics(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    trades: list[dict[str, Any]],
    exit_events: pd.DataFrame,
    *,
    strategy_name: str = "",
    variant: str = "",
    timeframe_minutes: int = 15,
    bars_per_year: int | None = None,
) -> StandardMetrics:
    """从回测产物计算所有标准绩效指标 (V2 增强版)。"""
    if bars_per_year is None:
        bars_per_year = int(BARS_PER_YEAR_15M * (15 / timeframe_minutes))

    m = StandardMetrics(
        strategy_name=strategy_name, variant=variant,
        computed_at=datetime.now().isoformat(timespec="seconds"),
    )

    if equity.empty:
        return m

    # ── 基础统计 ──
    m.bar_count = len(equity)
    m.fill_count = len(fills)
    m.trade_count = len(trades)

    if "datetime" in equity.columns:
        m.data_start = str(equity["datetime"].iloc[0])[:19]
        m.data_end = str(equity["datetime"].iloc[-1])[:19]

    # ── 权益曲线 (逐 bar) ──
    curve = equity["equity_points"].astype(float)
    m.total_return_points = float(curve.iloc[-1])
    m.max_drawdown_points = float((curve.cummax() - curve).max())

    if "close" in equity.columns and not equity["close"].empty:
        start_price = float(equity["close"].iloc[0])
        if start_price > 0:
            m.total_return_pct = m.total_return_points / start_price * 100

    # ── 年化 Calmar ──
    n_years = m.bar_count / bars_per_year if bars_per_year > 0 else 1
    annualized_return = (m.total_return_points / n_years) if n_years > 0 else 0
    m.annualized_return_pct = annualized_return / start_price * 100 if start_price > 0 else 0
    m.calmar_ratio = (
        abs(annualized_return / m.max_drawdown_points)
        if m.max_drawdown_points != 0 and n_years > 0 else 0
    )

    if not trades:
        return m

    # ── 交易统计 ──
    pnls = [t["pnl_points"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    m.win_rate = len(wins) / len(pnls)
    m.avg_win_points = sum(wins) / len(wins) if wins else 0.0
    m.avg_loss_points = sum(losses) / len(losses) if losses else 0.0
    m.avg_trade_points = sum(pnls) / len(pnls)
    m.payoff_ratio = abs(m.avg_win_points / m.avg_loss_points) if m.avg_loss_points != 0 else float("inf")
    m.expectancy = m.win_rate * m.avg_win_points - (1 - m.win_rate) * abs(m.avg_loss_points)

    m.largest_win_points = max(pnls) if pnls else 0.0
    m.largest_loss_points = min(pnls) if pnls else 0.0

    total_profit = sum(wins)
    total_loss = abs(sum(losses))
    m.profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

    # 连续胜/负
    cons_w = cons_l = max_cons_w = max_cons_l = 0
    for p in pnls:
        if p > 0:
            cons_w += 1; cons_l = 0
            max_cons_w = max(max_cons_w, cons_w)
        else:
            cons_l += 1; cons_w = 0
            max_cons_l = max(max_cons_l, cons_l)
    m.max_consecutive_wins = max_cons_w
    m.max_consecutive_losses = max_cons_l

    # 持仓时间
    hold_bars = [t.get("hold_bars", 0) for t in trades]
    m.avg_hold_bars = sum(hold_bars) / len(hold_bars)
    m.avg_hold_hours = m.avg_hold_bars * timeframe_minutes / 60

    # ── 收益分布 ──
    if len(pnls) >= 2:
        arr = np.array(pnls, dtype=float)
        m.std_trade_points = float(np.std(arr, ddof=1))
        mean_pnl = float(np.mean(arr))
        # skewness (adjusted for sample)
        n = len(arr)
        if m.std_trade_points > 0:
            m.skewness = float((np.sum((arr - mean_pnl) ** 3) / n) / (m.std_trade_points ** 3))
        # excess kurtosis
        if m.std_trade_points > 0:
            m.kurtosis = float((np.sum((arr - mean_pnl) ** 4) / n) / (m.std_trade_points ** 4)) - 3.0

    # ── MFE / MAE / giveback ──
    mfes = [t.get("mean_mfe_points", 0) or 0 for t in trades]
    maes = [t.get("mean_mae_points", 0) or 0 for t in trades]
    gbs = [t.get("mean_giveback_points", 0) or 0 for t in trades]
    m.avg_mfe_points = sum(mfes) / len(mfes) if mfes else 0.0
    m.avg_mae_points = sum(maes) / len(maes) if maes else 0.0
    m.avg_giveback_points = sum(gbs) / len(gbs) if gbs else 0.0

    # ── Sharpe / Sortino (bar-level equity returns annualized) ──
    bar_returns = curve.diff().dropna()
    if len(bar_returns) >= 2 and bar_returns.std() > 0:
        bar_mean = bar_returns.mean()
        bar_std = bar_returns.std()
        m.sharpe_ratio = (bar_mean / bar_std) * np.sqrt(bars_per_year)

        # Sortino: downside deviation from 0
        downside = bar_returns[bar_returns < 0]
        if len(downside) > 0:
            down_std = np.sqrt(np.mean(downside ** 2))
            m.sortino_ratio = (bar_mean / down_std) * np.sqrt(bars_per_year) if down_std > 0 else 0

    # ── 最大回撤持续期 ──
    dd_curve = curve.cummax() - curve
    in_dd = dd_curve > 0
    if in_dd.any():
        dd_duration = 0
        max_dd_duration = 0
        for v in in_dd:
            if v:
                dd_duration += 1
                max_dd_duration = max(max_dd_duration, dd_duration)
            else:
                dd_duration = 0
        m.max_drawdown_duration_bars = max_dd_duration
        m.max_drawdown_duration_hours = max_dd_duration * timeframe_minutes / 60

    # ── 成本分解 ──
    m.total_gross_points = m.total_return_points
    if not fills.empty and "fill_price" in fills.columns:
        # Count fills to estimate fees
        try:
            fee_per_side = 1.0  # default; can be overridden via config
            slip_per_side = 1.0
            total_fills = len(fills)
            m.total_fee_points = total_fills * fee_per_side
            m.total_slippage_points = total_fills * slip_per_side
            m.total_gross_points = m.total_return_points + m.total_fee_points + m.total_slippage_points
            if m.total_gross_points != 0:
                m.cost_pct_of_gross = (m.total_fee_points + m.total_slippage_points) / abs(m.total_gross_points) * 100
        except Exception:
            pass

    # ── 逐年收益和风险指标 ──
    if "datetime" in equity.columns and trades:
        _compute_yearly_metrics(m, equity, trades, bars_per_year, timeframe_minutes)

    # ── 出场分析 ──
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        m.exit_reason_breakdown[reason] = m.exit_reason_breakdown.get(reason, 0) + 1
        rule = t.get("exit_rule", "unknown")
        m.exit_rule_breakdown[rule] = m.exit_rule_breakdown.get(rule, 0) + 1

    return m


def _compute_yearly_metrics(
    m: StandardMetrics,
    equity: pd.DataFrame,
    trades: list[dict],
    bars_per_year: int,
    tf_minutes: int,
) -> None:
    """计算逐年收益和风险指标。"""
    # 按出场时间归年
    yr_trades: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        try:
            ts = pd.Timestamp(t.get("exit_time", t.get("entry_time")))
            yr = str(ts.year)
            yr_trades[yr].append(t)
        except Exception:
            pass

    # 按年切权益曲线
    if "datetime" in equity.columns:
        eq = equity.copy()
        eq["_dt"] = pd.to_datetime(eq["datetime"])
        eq["_yr"] = eq["_dt"].dt.year

    for yr, yr_t_list in sorted(yr_trades.items()):
        yr_pnls = [t["pnl_points"] for t in yr_t_list]
        m.return_by_year[yr] = sum(yr_pnls)
        m.yearly_trade_count[yr] = len(yr_t_list)

        yr_wins = [p for p in yr_pnls if p > 0]
        yr_losses = [p for p in yr_pnls if p <= 0]
        m.yearly_win_rate[yr] = len(yr_wins) / len(yr_pnls) if yr_pnls else 0
        total_profit = sum(yr_wins)
        total_loss = abs(sum(yr_losses))
        m.yearly_profit_factor[yr] = total_profit / total_loss if total_loss else float("inf")

        # Yearly DD from equity curve
        if "datetime" in equity.columns:
            yr_eq = eq[eq["_yr"] == int(yr)]
            if len(yr_eq) > 1:
                yr_curve = yr_eq["equity_points"].astype(float)
                yr_dd = float((yr_curve.cummax() - yr_curve).max())
                yr_bars = len(yr_eq)
                yr_years = max(yr_bars / bars_per_year, 0.01)
                yr_annual_return = sum(yr_pnls) / yr_years
                m.yearly_mar[yr] = abs(yr_annual_return / yr_dd) if yr_dd > 0 else 0

                yr_returns = yr_curve.diff().dropna()
                if len(yr_returns) >= 2 and yr_returns.std() > 0:
                    m.yearly_sharpe[yr] = (yr_returns.mean() / yr_returns.std()) * np.sqrt(bars_per_year)

    # 盈利年比例
    m.years_total = len(m.return_by_year)
    m.years_profitable = sum(1 for v in m.return_by_year.values() if v > 0)


# ════════════════════════════════════════════════════════════════
# 报告保存
# ════════════════════════════════════════════════════════════════

def save_standard_report(
    output_dir: Path,
    *,
    bars: pd.DataFrame,
    fills: pd.DataFrame,
    trades: list[dict[str, Any]],
    exit_events: pd.DataFrame,
    config: dict[str, Any],
    metrics: StandardMetrics,
) -> None:
    """将所有回测产物保存到标准报告目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    bars.to_csv(output_dir / "bars.csv", index=False, encoding="utf-8-sig")
    fills.to_csv(output_dir / "fills.csv", index=False, encoding="utf-8-sig")
    exit_events.to_csv(output_dir / "exit_events.csv", index=False, encoding="utf-8-sig")

    if trades:
        pd.DataFrame(trades).to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")

    metrics.to_dataframe().to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")

    with open(output_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, default=str, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════
# 对比报告
# ════════════════════════════════════════════════════════════════

def save_comparison_report(
    output_dir: Path,
    results: list[tuple[str, StandardMetrics]],
    *,
    sort_by: str = "total_return_points",
) -> pd.DataFrame:
    """保存并返回多策略对比的汇总表格。"""
    rows = []
    for label, m in results:
        rows.append({**m.to_dict(), "_label": label})

    df = pd.DataFrame(rows)
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False)

    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_dir / "comparison.csv", index=False, encoding="utf-8-sig")
    return df
