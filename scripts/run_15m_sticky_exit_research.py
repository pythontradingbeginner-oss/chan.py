from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_dc_structure_candidate_diagnostics import (
    CANDIDATES,
    _candidate_features,
    _naive_datetimes,
    _restrict_research_window,
)


PERIODS = ["2018-2022", "2023-2025YTD", "full_2018-2025YTD"]
TRAIL_TRIGGERS = [300.0, 500.0, 800.0]
GIVEBACK_RATIOS = [0.3, 0.4, 0.5]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research exits for the 15m sticky DC hard-filter candidate."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    candidate = next(
        item for item in CANDIDATES if item.name == "15m_sticky_dc30_entry15_exit30"
    )
    continuous = load_continuous_1m(args.continuous_path)
    bars = _restrict_research_window(
        aggregate_continuous_1m_to_Nm(continuous, candidate.freq_minutes)
    )
    featured = _candidate_features(bars, candidate)
    long_signal = featured["trend_macd_golden_cross"] & featured["dc_is_bull"]
    short_signal = featured["trend_macd_death_cross"] & featured["dc_is_bear"]

    strategies: list[pd.DataFrame] = []
    trades: list[pd.DataFrame] = []
    for config in _exit_configs():
        strategy = simulate_exit_strategy(
            featured,
            long_signal=long_signal,
            short_signal=short_signal,
            strategy_name=config["strategy"],
            reverse_mode=config["reverse_mode"],
            trail_trigger_points=config["trail_trigger_points"],
            giveback_ratio=config["giveback_ratio"],
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        strategies.append(strategy)
        trades.append(_extract_trades(featured, strategy, config))

    strategy_report = pd.concat(strategies, ignore_index=True)
    trade_report = pd.concat(trades, ignore_index=True)
    summary = _summary_rows(strategy_report, trade_report)
    yearly = _yearly_rows(trade_report)
    drawdowns = _drawdown_rows(strategy_report, trade_report)

    strategy_report.to_csv(
        args.reports_dir / "exit_research_15m_sticky_strategy_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trade_report.to_csv(
        args.reports_dir / "exit_research_15m_sticky_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "exit_research_15m_sticky_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "exit_research_15m_sticky_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    drawdowns.to_csv(
        args.reports_dir / "exit_research_15m_sticky_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")


def simulate_exit_strategy(
    featured: pd.DataFrame,
    *,
    long_signal: pd.Series,
    short_signal: pd.Series,
    strategy_name: str,
    reverse_mode: str,
    trail_trigger_points: float | None,
    giveback_ratio: float | None,
    fee_points: float,
    slippage_points: float,
    enable_structure_stop: bool = False,
) -> pd.DataFrame:
    if reverse_mode not in {"reverse", "flat"}:
        raise ValueError("reverse_mode must be one of: reverse, flat")
    if trail_trigger_points is not None and trail_trigger_points <= 0:
        raise ValueError("trail_trigger_points must be positive")
    if giveback_ratio is not None and not 0 < giveback_ratio < 1:
        raise ValueError("giveback_ratio must be between 0 and 1")
    if enable_structure_stop:
        required = [
            "dc_recent_valley_price",
            "dc_recent_valley_timestamp",
            "dc_recent_peak_price",
            "dc_recent_peak_timestamp",
        ]
        missing = [column for column in required if column not in featured.columns]
        if missing:
            raise ValueError(
                "featured missing structure stop column(s): " + ", ".join(missing)
            )

    closes = pd.to_numeric(featured["close"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(featured["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(featured["low"], errors="coerce").to_numpy(dtype=float)
    long_values = long_signal.fillna(False).to_numpy(dtype=bool)
    short_values = short_signal.fillna(False).to_numpy(dtype=bool)
    long_stop_prices = (
        pd.to_numeric(featured.get("dc_recent_valley_price"), errors="coerce").to_numpy(dtype=float)
        if enable_structure_stop
        else np.full(len(featured), np.nan, dtype=float)
    )
    short_stop_prices = (
        pd.to_numeric(featured.get("dc_recent_peak_price"), errors="coerce").to_numpy(dtype=float)
        if enable_structure_stop
        else np.full(len(featured), np.nan, dtype=float)
    )
    long_stop_timestamps = (
        featured["dc_recent_valley_timestamp"].to_numpy(dtype=object)
        if enable_structure_stop
        else np.full(len(featured), pd.NaT, dtype=object)
    )
    short_stop_timestamps = (
        featured["dc_recent_peak_timestamp"].to_numpy(dtype=object)
        if enable_structure_stop
        else np.full(len(featured), pd.NaT, dtype=object)
    )
    positions: list[float] = []
    exit_reasons: list[str] = []
    event_prices: list[float] = []
    strategy_returns: list[float] = []
    trail_mfe_values: list[float] = []
    trail_giveback_values: list[float] = []
    structure_stop_price_values: list[float] = []
    structure_stop_timestamp_values: list[object] = []
    structure_stop_source_price_values: list[float] = []
    structure_stop_adjusted_values: list[bool] = []

    cost_per_trade = fee_points + slippage_points
    position = 0.0
    entry_price = np.nan
    entry_row: int | None = None
    structure_stop_price = np.nan
    structure_stop_timestamp: object = pd.NaT
    structure_stop_source_price = np.nan
    structure_stop_adjusted = False
    best_close_move = 0.0
    for index, close in enumerate(closes):
        previous_close = closes[index - 1] if index > 0 else close
        start_position = position
        reason = "hold"
        event_price = close
        giveback = 0.0
        if start_position != 0 and not pd.isna(entry_price):
            current_move = position * (close - entry_price)
            best_close_move = max(best_close_move, current_move)
            giveback = best_close_move - current_move

        if enable_structure_stop:
            signal_used = False
            if reverse_mode == "reverse":
                if long_values[index]:
                    signal_used = True
                    if position != 1.0:
                        stop_snapshot = _structure_stop_snapshot(
                            side=1,
                            entry_price=close,
                            source_price=long_stop_prices[index],
                            source_timestamp=long_stop_timestamps[index],
                        )
                        position = 1.0
                        entry_price = close
                        entry_row = index
                        structure_stop_price = stop_snapshot["stop_price"]
                        structure_stop_timestamp = stop_snapshot["timestamp"]
                        structure_stop_source_price = stop_snapshot["source_price"]
                        structure_stop_adjusted = bool(stop_snapshot["adjusted"])
                        best_close_move = 0.0
                        reason = "long_signal"
                elif short_values[index]:
                    signal_used = True
                    if position != -1.0:
                        stop_snapshot = _structure_stop_snapshot(
                            side=-1,
                            entry_price=close,
                            source_price=short_stop_prices[index],
                            source_timestamp=short_stop_timestamps[index],
                        )
                        position = -1.0
                        entry_price = close
                        entry_row = index
                        structure_stop_price = stop_snapshot["stop_price"]
                        structure_stop_timestamp = stop_snapshot["timestamp"]
                        structure_stop_source_price = stop_snapshot["source_price"]
                        structure_stop_adjusted = bool(stop_snapshot["adjusted"])
                        best_close_move = 0.0
                        reason = "short_signal"
            elif position == 0.0:
                if long_values[index]:
                    signal_used = True
                    stop_snapshot = _structure_stop_snapshot(
                        side=1,
                        entry_price=close,
                        source_price=long_stop_prices[index],
                        source_timestamp=long_stop_timestamps[index],
                    )
                    position = 1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = stop_snapshot["stop_price"]
                    structure_stop_timestamp = stop_snapshot["timestamp"]
                    structure_stop_source_price = stop_snapshot["source_price"]
                    structure_stop_adjusted = bool(stop_snapshot["adjusted"])
                    best_close_move = 0.0
                    reason = "long_signal"
                elif short_values[index]:
                    signal_used = True
                    stop_snapshot = _structure_stop_snapshot(
                        side=-1,
                        entry_price=close,
                        source_price=short_stop_prices[index],
                        source_timestamp=short_stop_timestamps[index],
                    )
                    position = -1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = stop_snapshot["stop_price"]
                    structure_stop_timestamp = stop_snapshot["timestamp"]
                    structure_stop_source_price = stop_snapshot["source_price"]
                    structure_stop_adjusted = bool(stop_snapshot["adjusted"])
                    best_close_move = 0.0
                    reason = "short_signal"
            elif position > 0 and short_values[index]:
                signal_used = True
                position = 0.0
                entry_price = np.nan
                entry_row = None
                structure_stop_price = np.nan
                structure_stop_timestamp = pd.NaT
                structure_stop_source_price = np.nan
                structure_stop_adjusted = False
                best_close_move = 0.0
                reason = "opposite_flat_exit"
            elif position < 0 and long_values[index]:
                signal_used = True
                position = 0.0
                entry_price = np.nan
                entry_row = None
                structure_stop_price = np.nan
                structure_stop_timestamp = pd.NaT
                structure_stop_source_price = np.nan
                structure_stop_adjusted = False
                best_close_move = 0.0
                reason = "opposite_flat_exit"

            can_check_structure_stop = (
                not signal_used
                and start_position != 0
                and entry_row is not None
                and index > entry_row
                and not pd.isna(structure_stop_price)
            )
            if can_check_structure_stop and (
                (start_position > 0 and lows[index] <= structure_stop_price)
                or (start_position < 0 and highs[index] >= structure_stop_price)
            ):
                position = 0.0
                entry_price = np.nan
                entry_row = None
                event_price = float(structure_stop_price)
                structure_stop_price = np.nan
                structure_stop_timestamp = pd.NaT
                structure_stop_source_price = np.nan
                structure_stop_adjusted = False
                best_close_move = 0.0
                reason = "structure_stop_exit"
            elif not signal_used and (
                start_position != 0
                and trail_trigger_points is not None
                and giveback_ratio is not None
                and best_close_move >= trail_trigger_points
                and giveback >= best_close_move * giveback_ratio
            ):
                position = 0.0
                entry_price = np.nan
                entry_row = None
                structure_stop_price = np.nan
                structure_stop_timestamp = pd.NaT
                structure_stop_source_price = np.nan
                structure_stop_adjusted = False
                best_close_move = 0.0
                reason = "trail_exit"
        else:
            stopped = False
            if (
                start_position != 0
                and trail_trigger_points is not None
                and giveback_ratio is not None
                and best_close_move >= trail_trigger_points
                and giveback >= best_close_move * giveback_ratio
            ):
                position = 0.0
                entry_price = np.nan
                entry_row = None
                best_close_move = 0.0
                reason = "trail_exit"
                stopped = True

            if not stopped:
                if reverse_mode == "reverse":
                    if long_values[index]:
                        if position != 1.0:
                            entry_price = close
                            entry_row = index
                            best_close_move = 0.0
                            reason = "long_signal"
                        position = 1.0
                    elif short_values[index]:
                        if position != -1.0:
                            entry_price = close
                            entry_row = index
                            best_close_move = 0.0
                            reason = "short_signal"
                        position = -1.0
                else:
                    if position == 0.0:
                        if long_values[index]:
                            position = 1.0
                            entry_price = close
                            entry_row = index
                            best_close_move = 0.0
                            reason = "long_signal"
                        elif short_values[index]:
                            position = -1.0
                            entry_price = close
                            entry_row = index
                            best_close_move = 0.0
                            reason = "short_signal"
                    elif position > 0 and short_values[index]:
                        position = 0.0
                        entry_price = np.nan
                        entry_row = None
                        best_close_move = 0.0
                        reason = "opposite_flat_exit"
                    elif position < 0 and long_values[index]:
                        position = 0.0
                        entry_price = np.nan
                        entry_row = None
                        best_close_move = 0.0
                        reason = "opposite_flat_exit"

        if position == 0.0:
            entry_row = None
            if reason != "structure_stop_exit":
                structure_stop_price = np.nan
                structure_stop_timestamp = pd.NaT
                structure_stop_source_price = np.nan
                structure_stop_adjusted = False

        trade_size = abs(position - start_position)
        strategy_return = (
            start_position * (event_price - previous_close) - trade_size * cost_per_trade
        )

        positions.append(position)
        exit_reasons.append(reason)
        event_prices.append(float(event_price))
        strategy_returns.append(float(strategy_return))
        trail_mfe_values.append(best_close_move if position != 0.0 else 0.0)
        trail_giveback_values.append(giveback if position != 0.0 else 0.0)
        structure_stop_price_values.append(
            float(structure_stop_price) if position != 0.0 and not pd.isna(structure_stop_price) else np.nan
        )
        structure_stop_timestamp_values.append(structure_stop_timestamp if position != 0.0 else pd.NaT)
        structure_stop_source_price_values.append(
            float(structure_stop_source_price)
            if position != 0.0 and not pd.isna(structure_stop_source_price)
            else np.nan
        )
        structure_stop_adjusted_values.append(bool(structure_stop_adjusted) if position != 0.0 else False)

    result = featured[["datetime", "open", "high", "low", "close"]].copy()
    result["strategy"] = strategy_name
    result["reverse_mode"] = reverse_mode
    result["trail_trigger_points"] = trail_trigger_points
    result["giveback_ratio"] = giveback_ratio
    result["structure_stop_enabled"] = enable_structure_stop
    result["position"] = pd.Series(positions, index=featured.index, dtype="float64")
    result["event_reason"] = exit_reasons
    result["event_price"] = event_prices
    result["structure_stop_price"] = structure_stop_price_values
    result["structure_stop_timestamp"] = structure_stop_timestamp_values
    result["structure_stop_source_price"] = structure_stop_source_price_values
    result["structure_stop_adjusted"] = structure_stop_adjusted_values
    result["trail_close_mfe_points"] = trail_mfe_values
    result["trail_close_giveback_points"] = trail_giveback_values
    result["trade_size"] = result["position"].diff().abs().fillna(result["position"].abs())
    result["bar_return_points"] = result["close"].diff().fillna(0.0)
    result["strategy_return_points"] = strategy_returns
    result["equity_points"] = result["strategy_return_points"].cumsum()
    return result


def _structure_stop_snapshot(
    *,
    side: int,
    entry_price: float,
    source_price: float,
    source_timestamp: object,
) -> dict[str, object]:
    if side not in {1, -1}:
        raise ValueError("side must be 1 or -1")
    if pd.isna(source_price):
        return {
            "stop_price": np.nan,
            "timestamp": source_timestamp,
            "source_price": np.nan,
            "adjusted": False,
        }

    source = float(source_price)
    entry = float(entry_price)
    adjusted = False
    if side > 0:
        stop_price = source if source <= entry else entry - abs(entry - source)
        adjusted = source > entry
    else:
        stop_price = source if source >= entry else entry + abs(entry - source)
        adjusted = source < entry
    return {
        "stop_price": float(stop_price),
        "timestamp": source_timestamp,
        "source_price": source,
        "adjusted": adjusted,
    }


def _exit_configs() -> list[dict[str, object]]:
    configs: list[dict[str, object]] = [
        {
            "strategy": "signal_reverse",
            "reverse_mode": "reverse",
            "trail_trigger_points": None,
            "giveback_ratio": None,
        },
        {
            "strategy": "signal_flat",
            "reverse_mode": "flat",
            "trail_trigger_points": None,
            "giveback_ratio": None,
        },
    ]
    for reverse_mode in ["reverse", "flat"]:
        for trigger in TRAIL_TRIGGERS:
            for giveback in GIVEBACK_RATIOS:
                configs.append(
                    {
                        "strategy": f"{reverse_mode}_trail_{int(trigger)}_gb{int(giveback * 100)}",
                        "reverse_mode": reverse_mode,
                        "trail_trigger_points": trigger,
                        "giveback_ratio": giveback,
                    }
                )
    return configs


def _extract_trades(
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    config: dict[str, object],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    current_return = 0.0
    previous_sign = 0
    positions = strategy["position"].to_numpy(dtype=float, copy=False)
    returns = strategy["strategy_return_points"].to_numpy(dtype=float, copy=False)

    for row_number, (position, strategy_return) in enumerate(zip(positions, returns)):
        sign = int(np.sign(position))
        if current is None and sign != 0:
            current = _new_trade(featured, strategy, row_number, sign, config)
            current_return = 0.0

        if current is not None:
            current_return += float(strategy_return)

        if current is not None and previous_sign != 0 and sign != previous_sign:
            _close_trade(current, featured, strategy, row_number, current_return)
            rows.append(current)
            current = _new_trade(featured, strategy, row_number, sign, config) if sign != 0 else None
            current_return = 0.0

        previous_sign = sign

    return pd.DataFrame(rows)


def _new_trade(
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    row_number: int,
    side: int,
    config: dict[str, object],
) -> dict[str, object]:
    row = featured.iloc[row_number]
    strategy_row = strategy.iloc[row_number]
    return {
        "strategy": config["strategy"],
        "reverse_mode": config["reverse_mode"],
        "trail_trigger_points": config["trail_trigger_points"],
        "giveback_ratio": config["giveback_ratio"],
        "structure_stop_enabled": bool(strategy_row.get("structure_stop_enabled", False)),
        "entry_row": row_number,
        "entry_time": row["datetime"],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(row["close"]),
        "entry_reason": strategy_row["event_reason"],
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
            "holding_bars": exit_row - entry_row,
            "holding_minutes": (exit_row - entry_row) * 15,
            "gross_return_points": gross_return,
            "net_return_points": float(net_return_points),
            "cost_drag_points": gross_return - float(net_return_points),
            "mfe_points": mfe,
            "mae_points": mae,
            "giveback_from_mfe_points": mfe - gross_return,
        }
    )


def _summary_rows(strategy_report: pd.DataFrame, trade_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        trades = trade_report[trade_report["strategy"] == strategy_name]
        for period in PERIODS:
            period_strategy = strategy.loc[_period_mask(strategy, period)]
            period_trades = trades.loc[_trade_period_mask(trades, period)]
            rows.append(
                {
                    "strategy": strategy_name,
                    "period": period,
                    **_strategy_metrics(period_strategy),
                    **_trade_metrics(period_trades),
                }
            )
    return pd.DataFrame(rows)


def _yearly_rows(trade_report: pd.DataFrame) -> pd.DataFrame:
    if trade_report.empty:
        return pd.DataFrame()
    trades = trade_report.copy()
    trades["year"] = _naive_datetimes(trades["entry_time"]).dt.year
    rows = []
    for (strategy_name, year), group in trades.groupby(["strategy", "year"], sort=True):
        rows.append(
            {
                "strategy": strategy_name,
                "year": int(year),
                **_trade_metrics(group),
            }
        )
    return pd.DataFrame(rows)


def _drawdown_rows(strategy_report: pd.DataFrame, trade_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        equity = strategy["equity_points"].reset_index(drop=True)
        running_peak = equity.cummax()
        drawdown = running_peak - equity
        intervals: list[tuple[int, int]] = []
        trades = trade_report[trade_report["strategy"] == strategy_name]
        strategy_rank = 0
        for trough in drawdown.sort_values(ascending=False).index:
            if drawdown.loc[trough] <= 0:
                break
            peak = int(equity.loc[:trough].idxmax())
            interval = (peak, int(trough))
            if any(not (interval[1] < used[0] or interval[0] > used[1]) for used in intervals):
                continue
            intervals.append(interval)
            strategy_rank += 1
            overlapping = trades[
                (trades["entry_row"] <= interval[1]) & (trades["exit_row"] >= interval[0])
            ]
            rows.append(
                {
                    "strategy": strategy_name,
                    "rank": strategy_rank,
                    "peak_time": strategy.iloc[interval[0]]["datetime"],
                    "trough_time": strategy.iloc[interval[1]]["datetime"],
                    "drawdown_points": float(drawdown.loc[trough]),
                    "duration_bars": interval[1] - interval[0],
                    "overlapping_trade_count": float(len(overlapping)),
                    "overlapping_trade_return_points": (
                        float(overlapping["net_return_points"].sum()) if len(overlapping) else 0.0
                    ),
                }
            )
            if strategy_rank >= 3:
                break
    return pd.DataFrame(rows)


def _strategy_metrics(strategy: pd.DataFrame) -> dict[str, float]:
    if strategy.empty:
        return {
            "total_return_points": 0.0,
            "max_drawdown_points": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "avg_abs_position": 0.0,
        }
    returns = strategy["strategy_return_points"]
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    total_return = float(returns.sum())
    volatility = float(returns.std(ddof=0))
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0
    return {
        "total_return_points": total_return,
        "max_drawdown_points": max_drawdown,
        "sharpe": float(returns.mean() / volatility * np.sqrt(252)) if volatility else 0.0,
        "calmar": float(total_return / max_drawdown) if max_drawdown else 0.0,
        "avg_abs_position": float(strategy["position"].abs().mean()),
    }


def _trade_metrics(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {
            "trade_count": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "mean_trade_points": 0.0,
            "median_trade_points": 0.0,
            "mean_mfe_points": 0.0,
            "mean_mae_points": 0.0,
            "mean_giveback_points": 0.0,
            "median_holding_bars": 0.0,
        }
    returns = trades["net_return_points"]
    return {
        "trade_count": float(len(trades)),
        "win_rate": float((returns > 0).mean()),
        "profit_factor": _profit_factor(returns),
        "mean_trade_points": float(returns.mean()),
        "median_trade_points": float(returns.median()),
        "mean_mfe_points": float(trades["mfe_points"].mean()),
        "mean_mae_points": float(trades["mae_points"].mean()),
        "mean_giveback_points": float(trades["giveback_from_mfe_points"].mean()),
        "median_holding_bars": float(trades["holding_bars"].median()),
    }


def _profit_factor(returns: pd.Series) -> float:
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if wins.empty or losses.empty:
        return 0.0
    return float(wins.sum() / abs(losses.sum()))


def _period_mask(frame: pd.DataFrame, period: str) -> pd.Series:
    datetimes = _naive_datetimes(frame["datetime"])
    if period == "2018-2022":
        return (datetimes >= pd.Timestamp("2018-01-01")) & (
            datetimes <= pd.Timestamp("2022-12-31 23:59:59")
        )
    if period == "2023-2025YTD":
        return datetimes >= pd.Timestamp("2023-01-01")
    if period == "full_2018-2025YTD":
        return datetimes >= pd.Timestamp("2018-01-01")
    raise ValueError(f"unsupported period: {period}")


def _trade_period_mask(frame: pd.DataFrame, period: str) -> pd.Series:
    if frame.empty:
        return pd.Series(False, index=frame.index)
    datetimes = _naive_datetimes(frame["entry_time"])
    if period == "2018-2022":
        return (datetimes >= pd.Timestamp("2018-01-01")) & (
            datetimes <= pd.Timestamp("2022-12-31 23:59:59")
        )
    if period == "2023-2025YTD":
        return datetimes >= pd.Timestamp("2023-01-01")
    if period == "full_2018-2025YTD":
        return datetimes >= pd.Timestamp("2018-01-01")
    raise ValueError(f"unsupported period: {period}")


if __name__ == "__main__":
    main()
