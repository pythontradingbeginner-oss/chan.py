from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from data_foundation import (
    aggregate_continuous_1m_to_Nm,
    build_continuous_contract,
    clean_rb_1m_bars,
    load_raw_from_vnpy,
)
from run_15m_sticky_exit_research import simulate_exit_strategy
from run_dc_structure_candidate_diagnostics import Candidate, _candidate_features


START = "2018-01-15 21:01:00"
END = "2025-04-03 15:00:00"
FINAL_STRATEGY = "15m_sticky_dc30_entry15_exit30_reverse_trail_800_gb50"


FINAL_CANDIDATE = Candidate(
    name="15m_sticky_dc30_entry15_exit30",
    freq_minutes=15,
    trend_atr_k=0.7,
    trend_atr_window=50,
    dc_threshold_points=30.0,
    dc_mode="sticky",
    dc_entry_buffer_points=15.0,
    dc_exit_buffer_points=30.0,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest final 15m sticky DC trend-MACD strategy from local vn.py database."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(r"C:\Users\Administrator\.vntrader\database.db"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    raw = load_raw_from_vnpy(args.db_path, start=start.to_pydatetime(), end=end.to_pydatetime())
    cleaned, cleaning_log = clean_rb_1m_bars(raw)
    continuous, switch_log = build_continuous_contract(cleaned, method="backward_adjusted")
    continuous = _restrict_range(continuous, start, end)
    bars_15m = aggregate_continuous_1m_to_Nm(continuous, FINAL_CANDIDATE.freq_minutes)
    bars_15m = _restrict_range(bars_15m, start, end)

    featured = _candidate_features(bars_15m, FINAL_CANDIDATE)
    long_signal = featured["trend_macd_golden_cross"] & featured["dc_is_bull"]
    short_signal = featured["trend_macd_death_cross"] & featured["dc_is_bear"]
    strategy = simulate_exit_strategy(
        featured,
        long_signal=long_signal,
        short_signal=short_signal,
        strategy_name=FINAL_STRATEGY,
        reverse_mode="reverse",
        trail_trigger_points=800.0,
        giveback_ratio=0.5,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )
    trades = _extract_trades(featured, strategy)

    summary = _summary_frame(
        raw=raw,
        cleaned=cleaned,
        continuous=continuous,
        bars_15m=bars_15m,
        featured=featured,
        strategy=strategy,
        trades=trades,
        cleaning_log=cleaning_log,
        switch_log=switch_log,
        start=start,
        end=end,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )
    yearly = _yearly_frame(strategy, trades)
    drawdowns = _drawdown_frame(strategy, trades)

    strategy.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    drawdowns.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    switch_log.to_csv(
        args.reports_dir / "final_15m_strategy_vnpy_switch_log.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")


def _restrict_range(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    result["datetime"] = _naive_datetimes(result["datetime"])
    return result[(result["datetime"] >= start) & (result["datetime"] <= end)].reset_index(drop=True)


def _extract_trades(featured: pd.DataFrame, strategy: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    current_return = 0.0
    previous_sign = 0
    positions = strategy["position"].to_numpy(dtype=float, copy=False)
    returns = strategy["strategy_return_points"].to_numpy(dtype=float, copy=False)

    for row_number, (position, strategy_return) in enumerate(zip(positions, returns)):
        sign = int(np.sign(position))
        if current is None and sign != 0:
            current = _new_trade(featured, strategy, row_number, sign)
            current_return = 0.0

        if current is not None:
            current_return += float(strategy_return)

        if current is not None and previous_sign != 0 and sign != previous_sign:
            _close_trade(current, featured, strategy, row_number, current_return)
            rows.append(current)
            current = _new_trade(featured, strategy, row_number, sign) if sign != 0 else None
            current_return = 0.0

        previous_sign = sign

    return pd.DataFrame(rows)


def _new_trade(
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    row_number: int,
    side: int,
) -> dict[str, object]:
    row = featured.iloc[row_number]
    strategy_row = strategy.iloc[row_number]
    return {
        "strategy": FINAL_STRATEGY,
        "entry_row": row_number,
        "entry_time": row["datetime"],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(row["close"]),
        "entry_reason": strategy_row["event_reason"],
        "entry_dc_state": row["dc_trend_state"],
        "entry_active_symbol": row.get("active_symbol", ""),
        "entry_structure_stop_price": (
            float(strategy_row["structure_stop_price"])
            if "structure_stop_price" in strategy_row
            and pd.notna(strategy_row["structure_stop_price"])
            else np.nan
        ),
        "entry_structure_stop_timestamp": (
            strategy_row["structure_stop_timestamp"]
            if "structure_stop_timestamp" in strategy_row
            else pd.NaT
        ),
        "entry_structure_stop_source_price": (
            float(strategy_row["structure_stop_source_price"])
            if "structure_stop_source_price" in strategy_row
            and pd.notna(strategy_row["structure_stop_source_price"])
            else np.nan
        ),
        "entry_structure_stop_adjusted": (
            bool(strategy_row["structure_stop_adjusted"])
            if "structure_stop_adjusted" in strategy_row
            else False
        ),
    }


def _close_trade(
    trade: dict[str, object],
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    exit_row: int,
    net_return_points: float,
) -> None:
    entry_row = int(trade["entry_row"])
    side = int(trade["side"])
    entry_close = float(trade["entry_close"])
    exit_record = featured.iloc[exit_row]
    exit_strategy_record = strategy.iloc[exit_row]
    exit_price = float(exit_strategy_record.get("event_price", exit_record["close"]))
    post_entry = featured.iloc[entry_row + 1 : exit_row + 1]
    if post_entry.empty:
        mfe = 0.0
        mae = 0.0
    elif side > 0:
        mfe = float((post_entry["high"] - entry_close).max())
        mae = float((post_entry["low"] - entry_close).min())
    else:
        mfe = float((entry_close - post_entry["low"]).max())
        mae = float((entry_close - post_entry["high"]).min())

    gross_return = side * (exit_price - entry_close)
    trade.update(
        {
            "exit_row": exit_row,
            "exit_time": exit_record["datetime"],
            "exit_close": exit_price,
            "exit_reason": exit_strategy_record["event_reason"],
            "exit_dc_state": exit_record["dc_trend_state"],
            "exit_active_symbol": exit_record.get("active_symbol", ""),
            "holding_bars": exit_row - entry_row,
            "holding_minutes": (exit_row - entry_row) * FINAL_CANDIDATE.freq_minutes,
            "gross_return_points": gross_return,
            "net_return_points": float(net_return_points),
            "cost_drag_points": gross_return - float(net_return_points),
            "mfe_points": mfe,
            "mae_points": mae,
            "giveback_from_mfe_points": mfe - gross_return,
        }
    )


def _summary_frame(
    *,
    raw: pd.DataFrame,
    cleaned: pd.DataFrame,
    continuous: pd.DataFrame,
    bars_15m: pd.DataFrame,
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    trades: pd.DataFrame,
    cleaning_log: object,
    switch_log: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    metrics = _strategy_metrics(strategy, trades)
    signal_counts = {
        "trend_macd_golden_cross": int(featured["trend_macd_golden_cross"].sum()),
        "trend_macd_death_cross": int(featured["trend_macd_death_cross"].sum()),
        "dc_bull_rows": int(featured["dc_is_bull"].sum()),
        "dc_bear_rows": int(featured["dc_is_bear"].sum()),
        "accepted_long_signals": int(
            (featured["trend_macd_golden_cross"] & featured["dc_is_bull"]).sum()
        ),
        "accepted_short_signals": int(
            (featured["trend_macd_death_cross"] & featured["dc_is_bear"]).sum()
        ),
        "trail_exit_count": int((strategy["event_reason"] == "trail_exit").sum()),
    }
    row = {
        "strategy": FINAL_STRATEGY,
        "requested_start": start,
        "requested_end": end,
        "raw_rows": float(len(raw)),
        "cleaned_rows": float(len(cleaned)),
        "continuous_rows": float(len(continuous)),
        "bars_15m": float(len(bars_15m)),
        "first_continuous_datetime": (
            continuous["datetime"].min() if not continuous.empty else pd.NaT
        ),
        "last_continuous_datetime": (
            continuous["datetime"].max() if not continuous.empty else pd.NaT
        ),
        "first_15m_datetime": bars_15m["datetime"].min() if not bars_15m.empty else pd.NaT,
        "last_15m_datetime": bars_15m["datetime"].max() if not bars_15m.empty else pd.NaT,
        "fee_points": fee_points,
        "slippage_points": slippage_points,
        "switch_count": float(len(switch_log)),
        "cleaning_raw_rows": float(cleaning_log.raw_rows),
        "cleaning_final_rows": float(cleaning_log.final_rows),
        **signal_counts,
        **metrics,
    }
    return pd.DataFrame([row])


def _strategy_metrics(strategy: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
    returns = strategy["strategy_return_points"]
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    total_return = float(returns.sum())
    volatility = float(returns.std(ddof=0))
    downside = returns[returns < 0]
    downside_std = float(downside.std(ddof=0))
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0
    trade_returns = trades["net_return_points"] if not trades.empty else pd.Series(dtype=float)
    wins = trade_returns[trade_returns > 0]
    losses = trade_returns[trade_returns < 0]
    gross_profit = float(wins.sum()) if not wins.empty else 0.0
    gross_loss = float(losses.sum()) if not losses.empty else 0.0
    return {
        "total_return_points": total_return,
        "max_drawdown_points": max_drawdown,
        "sharpe_sqrt252": float(returns.mean() / volatility * np.sqrt(252)) if volatility else 0.0,
        "sortino_sqrt252": (
            float(returns.mean() / downside_std * np.sqrt(252)) if downside_std else 0.0
        ),
        "calmar": float(total_return / max_drawdown) if max_drawdown else 0.0,
        "avg_abs_position": float(strategy["position"].abs().mean()),
        "trade_count": float(len(trades)),
        "win_count": float(len(wins)),
        "loss_count": float(len(losses)),
        "win_rate": float(len(wins) / len(trades)) if len(trades) else 0.0,
        "gross_profit_points": gross_profit,
        "gross_loss_points": gross_loss,
        "profit_factor": float(gross_profit / abs(gross_loss)) if gross_loss else 0.0,
        "avg_win_points": float(wins.mean()) if not wins.empty else 0.0,
        "avg_loss_points": float(losses.mean()) if not losses.empty else 0.0,
        "payoff_ratio": (
            float(wins.mean() / abs(losses.mean())) if not wins.empty and not losses.empty else 0.0
        ),
        "expectancy_points": float(trade_returns.mean()) if len(trade_returns) else 0.0,
        "median_trade_points": float(trade_returns.median()) if len(trade_returns) else 0.0,
        "largest_win_points": float(trade_returns.max()) if len(trade_returns) else 0.0,
        "largest_loss_points": float(trade_returns.min()) if len(trade_returns) else 0.0,
        "mean_mfe_points": float(trades["mfe_points"].mean()) if not trades.empty else 0.0,
        "mean_mae_points": float(trades["mae_points"].mean()) if not trades.empty else 0.0,
        "mean_giveback_points": (
            float(trades["giveback_from_mfe_points"].mean()) if not trades.empty else 0.0
        ),
        "median_holding_bars": (
            float(trades["holding_bars"].median()) if not trades.empty else 0.0
        ),
        "mean_holding_bars": float(trades["holding_bars"].mean()) if not trades.empty else 0.0,
    }


def _yearly_frame(strategy: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    if strategy.empty:
        return pd.DataFrame()
    strategy_with_year = strategy.copy()
    strategy_with_year["year"] = _naive_datetimes(strategy_with_year["datetime"]).dt.year
    trades_with_year = trades.copy()
    if not trades_with_year.empty:
        trades_with_year["year"] = _naive_datetimes(trades_with_year["entry_time"]).dt.year
    rows = []
    for year, year_strategy in strategy_with_year.groupby("year", sort=True):
        year_trades = (
            trades_with_year[trades_with_year["year"] == year]
            if not trades_with_year.empty
            else pd.DataFrame()
        )
        rows.append({"year": int(year), **_strategy_metrics(year_strategy, year_trades)})
    return pd.DataFrame(rows)


def _drawdown_frame(strategy: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    equity = strategy["equity_points"].reset_index(drop=True)
    running_peak = equity.cummax()
    drawdown = running_peak - equity
    rows = []
    intervals: list[tuple[int, int]] = []
    for trough in drawdown.sort_values(ascending=False).index:
        if drawdown.loc[trough] <= 0:
            break
        peak = int(equity.loc[:trough].idxmax())
        interval = (peak, int(trough))
        if any(not (interval[1] < used[0] or interval[0] > used[1]) for used in intervals):
            continue
        intervals.append(interval)
        overlapping = trades[(trades["entry_row"] <= interval[1]) & (trades["exit_row"] >= interval[0])]
        rows.append(
            {
                "rank": len(rows) + 1,
                "peak_row": interval[0],
                "trough_row": interval[1],
                "peak_time": strategy.loc[interval[0], "datetime"],
                "trough_time": strategy.loc[interval[1], "datetime"],
                "peak_equity_points": float(equity.loc[interval[0]]),
                "trough_equity_points": float(equity.loc[interval[1]]),
                "drawdown_points": float(drawdown.loc[trough]),
                "duration_bars": interval[1] - interval[0],
                "duration_minutes": (interval[1] - interval[0]) * FINAL_CANDIDATE.freq_minutes,
                "overlapping_trade_count": float(len(overlapping)),
                "overlapping_trade_return_points": (
                    float(overlapping["net_return_points"].sum()) if not overlapping.empty else 0.0
                ),
            }
        )
        if len(rows) >= 10:
            break
    return pd.DataFrame(rows)


def _naive_datetimes(values: pd.Series) -> pd.Series:
    result = pd.to_datetime(values)
    if getattr(result.dt, "tz", None) is not None:
        result = result.dt.tz_localize(None)
    return result


if __name__ == "__main__":
    main()
