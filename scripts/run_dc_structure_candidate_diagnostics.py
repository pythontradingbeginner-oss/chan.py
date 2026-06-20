from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from data_foundation import (
    add_dc_pivots,
    add_trend_macd,
    aggregate_continuous_1m_to_Nm,
    load_continuous_1m,
)


RESEARCH_START = "2018-01-01"


@dataclass(frozen=True)
class Candidate:
    name: str
    freq_minutes: int
    trend_atr_k: float
    trend_atr_window: int
    dc_threshold_points: float
    dc_mode: str
    dc_entry_buffer_points: float
    dc_exit_buffer_points: float | None = None


CANDIDATES = [
    Candidate(
        name="5m_strict_dc30_buffer5",
        freq_minutes=5,
        trend_atr_k=1.0,
        trend_atr_window=50,
        dc_threshold_points=30.0,
        dc_mode="strict",
        dc_entry_buffer_points=5.0,
    ),
    Candidate(
        name="15m_sticky_dc30_entry15_exit30",
        freq_minutes=15,
        trend_atr_k=0.7,
        trend_atr_window=50,
        dc_threshold_points=30.0,
        dc_mode="sticky",
        dc_entry_buffer_points=15.0,
        dc_exit_buffer_points=30.0,
    ),
]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose trade quality for selected DC structure trend-MACD candidates."
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

    continuous = load_continuous_1m(args.continuous_path)
    all_trades: list[pd.DataFrame] = []
    all_summary: list[dict[str, object]] = []
    all_yearly: list[dict[str, object]] = []
    all_drawdowns: list[dict[str, object]] = []
    all_holding: list[dict[str, object]] = []

    for candidate in CANDIDATES:
        bars = _restrict_research_window(
            aggregate_continuous_1m_to_Nm(continuous, candidate.freq_minutes)
        )
        featured = _candidate_features(bars, candidate)
        strategy = _evaluate_candidate(
            featured,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        trades = _extract_closed_trades(featured, strategy, candidate)
        all_trades.append(trades)
        all_summary.append(_summary_row(candidate, strategy, trades))
        all_yearly.extend(_yearly_rows(candidate, trades))
        all_drawdowns.extend(_top_drawdown_rows(candidate, strategy, trades))
        all_holding.extend(_holding_bucket_rows(candidate, trades))

    trades_report = pd.concat(all_trades, ignore_index=True)
    trades_report.to_csv(
        args.reports_dir / "dc_structure_candidate_diagnostics_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_summary).to_csv(
        args.reports_dir / "dc_structure_candidate_diagnostics_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_yearly).to_csv(
        args.reports_dir / "dc_structure_candidate_diagnostics_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_drawdowns).to_csv(
        args.reports_dir / "dc_structure_candidate_diagnostics_drawdowns.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_holding).to_csv(
        args.reports_dir / "dc_structure_candidate_diagnostics_holding_buckets.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (args.reports_dir / "dc_structure_candidate_diagnostics_candidates.json").write_text(
        json.dumps([asdict(candidate) for candidate in CANDIDATES], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"saved reports to: {args.reports_dir}")


def _candidate_features(bars: pd.DataFrame, candidate: Candidate) -> pd.DataFrame:
    featured = add_trend_macd(
        bars,
        threshold_type="atr_ratio",
        atr_k=candidate.trend_atr_k,
        atr_window=candidate.trend_atr_window,
        fast_period=12,
        slow_period=26,
        signal_period=9,
    )
    pivoted = add_dc_pivots(featured, threshold_points=candidate.dc_threshold_points)
    return _trend_state_from_confirmed_pivots(
        pivoted,
        mode=candidate.dc_mode,
        entry_buffer_points=candidate.dc_entry_buffer_points,
        exit_buffer_points=(
            candidate.dc_entry_buffer_points
            if candidate.dc_exit_buffer_points is None
            else candidate.dc_exit_buffer_points
        ),
    )


def _trend_state_from_confirmed_pivots(
    frame: pd.DataFrame,
    *,
    mode: str,
    entry_buffer_points: float,
    exit_buffer_points: float,
) -> pd.DataFrame:
    if mode not in {"strict", "sticky"}:
        raise ValueError("mode must be one of: strict, sticky")

    states = np.full(len(frame), "unknown", dtype=object)
    structure_ids = np.full(len(frame), np.nan, dtype=object)
    structure_states = np.full(len(frame), "unknown", dtype=object)
    left_valley_prices = np.full(len(frame), np.nan, dtype=float)
    left_valley_timestamps = np.full(len(frame), pd.NaT, dtype=object)
    left_peak_prices = np.full(len(frame), np.nan, dtype=float)
    left_peak_timestamps = np.full(len(frame), pd.NaT, dtype=object)
    peaks: list[dict[str, object]] = []
    valleys: list[dict[str, object]] = []
    current_state = "unknown"
    current_structure_id: int | None = None
    next_structure_id = 1
    current_left_valley: dict[str, object] | None = None
    current_left_peak: dict[str, object] | None = None
    previous_row = 0

    def write_segment(start: int, stop: int) -> None:
        if start >= stop:
            return
        states[start:stop] = current_state
        structure_states[start:stop] = current_state
        if current_structure_id is not None:
            structure_ids[start:stop] = current_structure_id
        if current_left_valley is not None:
            left_valley_prices[start:stop] = float(current_left_valley["price"])
            left_valley_timestamps[start:stop] = current_left_valley["timestamp"]
        if current_left_peak is not None:
            left_peak_prices[start:stop] = float(current_left_peak["price"])
            left_peak_timestamps[start:stop] = current_left_peak["timestamp"]

    pivot_rows = frame.index[frame["dc_pivot_kind"].notna()].to_list()
    for row_number in pivot_rows:
        write_segment(previous_row, row_number)
        kind = frame.at[row_number, "dc_pivot_kind"]
        event = {
            "price": float(frame.at[row_number, "dc_pivot_price"]),
            "timestamp": frame.at[row_number, "dc_pivot_timestamp"],
        }
        if kind == "peak":
            peaks.append(event)
        elif kind == "valley":
            valleys.append(event)
        else:
            raise ValueError(f"unsupported pivot kind: {kind}")

        if len(peaks) >= 2 and len(valleys) >= 2:
            next_state = _classify_pivot_structure(
                float(peaks[-2]["price"]),
                float(peaks[-1]["price"]),
                float(valleys[-2]["price"]),
                float(valleys[-1]["price"]),
                current_state=current_state,
                mode=mode,
                entry_buffer_points=entry_buffer_points,
                exit_buffer_points=exit_buffer_points,
            )
            if next_state != current_state:
                current_structure_id = next_structure_id
                next_structure_id += 1
                current_left_valley = valleys[-2] if next_state == "bull" else None
                current_left_peak = peaks[-2] if next_state == "bear" else None
            current_state = next_state

        write_segment(row_number, row_number + 1)
        previous_row = row_number + 1

    if previous_row < len(frame):
        write_segment(previous_row, len(frame))

    result = frame.copy()
    result["dc_trend_state"] = states
    result["dc_structure_id"] = structure_ids
    result["dc_structure_state"] = structure_states
    result["dc_structure_left_valley_price"] = left_valley_prices
    result["dc_structure_left_valley_timestamp"] = left_valley_timestamps
    result["dc_structure_left_peak_price"] = left_peak_prices
    result["dc_structure_left_peak_timestamp"] = left_peak_timestamps
    _append_recent_confirmed_pivots(result)
    result["dc_is_bull"] = result["dc_trend_state"] == "bull"
    result["dc_is_bear"] = result["dc_trend_state"] == "bear"
    return result


def _append_recent_confirmed_pivots(result: pd.DataFrame) -> None:
    pivot_prices = pd.to_numeric(result["dc_pivot_price"], errors="coerce")
    peak_rows = result["dc_pivot_kind"] == "peak"
    valley_rows = result["dc_pivot_kind"] == "valley"

    result["dc_recent_peak_price"] = pivot_prices.where(peak_rows).ffill()
    result["dc_recent_valley_price"] = pivot_prices.where(valley_rows).ffill()
    result["dc_recent_peak_timestamp"] = result["dc_pivot_timestamp"].where(peak_rows).ffill()
    result["dc_recent_valley_timestamp"] = result["dc_pivot_timestamp"].where(valley_rows).ffill()


def _classify_pivot_structure(
    previous_peak: float,
    latest_peak: float,
    previous_valley: float,
    latest_valley: float,
    *,
    current_state: str,
    mode: str,
    entry_buffer_points: float,
    exit_buffer_points: float,
) -> str:
    peak_delta = latest_peak - previous_peak
    valley_delta = latest_valley - previous_valley

    if mode == "strict":
        if peak_delta >= entry_buffer_points and valley_delta >= entry_buffer_points:
            return "bull"
        if peak_delta <= -entry_buffer_points and valley_delta <= -entry_buffer_points:
            return "bear"
        return "sideways"

    peaks_rising_entry = peak_delta >= entry_buffer_points
    valleys_rising_entry = valley_delta >= entry_buffer_points
    peaks_falling_entry = peak_delta <= -entry_buffer_points
    valleys_falling_entry = valley_delta <= -entry_buffer_points
    peaks_rising_exit = peak_delta >= exit_buffer_points
    valleys_rising_exit = valley_delta >= exit_buffer_points
    peaks_falling_exit = peak_delta <= -exit_buffer_points
    valleys_falling_exit = valley_delta <= -exit_buffer_points

    if current_state == "bull":
        return "sideways" if peaks_falling_exit and valleys_falling_exit else "bull"
    if current_state == "bear":
        return "sideways" if peaks_rising_exit and valleys_rising_exit else "bear"

    bull_leg = peaks_rising_entry or valleys_rising_entry
    bear_leg = peaks_falling_entry or valleys_falling_entry
    if bull_leg and not bear_leg:
        return "bull"
    if bear_leg and not bull_leg:
        return "bear"
    return "sideways"


def _evaluate_candidate(
    featured: pd.DataFrame,
    *,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    long_signal = featured["trend_macd_golden_cross"] & featured["dc_is_bull"]
    short_signal = featured["trend_macd_death_cross"] & featured["dc_is_bear"]
    position = _signal_hold_position(long_signal, short_signal)
    result = featured[["datetime", "open", "high", "low", "close"]].copy()
    result["position"] = position.astype("float64")
    result["trade_size"] = result["position"].diff().abs().fillna(result["position"].abs())
    result["bar_return_points"] = result["close"].diff().fillna(0.0)
    result["strategy_return_points"] = (
        result["position"].shift(1).fillna(0.0) * result["bar_return_points"]
        - result["trade_size"] * (fee_points + slippage_points)
    )
    result["equity_points"] = result["strategy_return_points"].cumsum()
    return result


def _extract_closed_trades(
    featured: pd.DataFrame,
    strategy: pd.DataFrame,
    candidate: Candidate,
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
            current = _new_trade(featured, row_number, sign, candidate)
            current_return = 0.0

        if current is not None:
            current_return += float(strategy_return)

        if current is not None and previous_sign != 0 and sign != previous_sign:
            _close_trade(current, featured, row_number, current_return)
            rows.append(current)
            current = _new_trade(featured, row_number, sign, candidate) if sign != 0 else None
            current_return = 0.0

        previous_sign = sign

    return pd.DataFrame(rows)


def _new_trade(
    featured: pd.DataFrame,
    row_number: int,
    side: int,
    candidate: Candidate,
) -> dict[str, object]:
    row = featured.iloc[row_number]
    return {
        "candidate": candidate.name,
        "freq_minutes": candidate.freq_minutes,
        "dc_mode": candidate.dc_mode,
        "dc_threshold_points": candidate.dc_threshold_points,
        "dc_entry_buffer_points": candidate.dc_entry_buffer_points,
        "dc_exit_buffer_points": candidate.dc_exit_buffer_points,
        "entry_row": row_number,
        "entry_time": row["datetime"],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(row["close"]),
        "entry_dc_state": row["dc_trend_state"],
        "entry_trend_move": float(row["trend_move"]) if pd.notna(row["trend_move"]) else np.nan,
        "entry_trend_threshold": (
            float(row["trend_threshold"]) if pd.notna(row["trend_threshold"]) else np.nan
        ),
    }


def _close_trade(
    trade: dict[str, object],
    featured: pd.DataFrame,
    exit_row: int,
    net_return_points: float,
) -> None:
    entry_row = int(trade["entry_row"])
    side = int(trade["side"])
    entry_close = float(trade["entry_close"])
    exit_record = featured.iloc[exit_row]
    post_entry = featured.iloc[entry_row + 1 : exit_row + 1]
    if post_entry.empty:
        favorable_excursion = 0.0
        adverse_excursion = 0.0
        close_favorable_excursion = 0.0
        close_adverse_excursion = 0.0
    elif side > 0:
        favorable_excursion = float((post_entry["high"] - entry_close).max())
        adverse_excursion = float((post_entry["low"] - entry_close).min())
        close_move = post_entry["close"] - entry_close
        close_favorable_excursion = float(close_move.max())
        close_adverse_excursion = float(close_move.min())
    else:
        favorable_excursion = float((entry_close - post_entry["low"]).max())
        adverse_excursion = float((entry_close - post_entry["high"]).min())
        close_move = entry_close - post_entry["close"]
        close_favorable_excursion = float(close_move.max())
        close_adverse_excursion = float(close_move.min())

    gross_return = side * (float(exit_record["close"]) - entry_close)
    trade.update(
        {
            "exit_row": exit_row,
            "exit_time": exit_record["datetime"],
            "exit_close": float(exit_record["close"]),
            "exit_dc_state": exit_record["dc_trend_state"],
            "holding_bars": exit_row - entry_row,
            "holding_minutes": (exit_row - entry_row) * int(trade["freq_minutes"]),
            "gross_return_points": gross_return,
            "net_return_points": float(net_return_points),
            "cost_drag_points": gross_return - float(net_return_points),
            "mfe_points": favorable_excursion,
            "mae_points": adverse_excursion,
            "close_mfe_points": close_favorable_excursion,
            "close_mae_points": close_adverse_excursion,
            "giveback_from_mfe_points": favorable_excursion - gross_return,
            "return_over_mfe": _safe_ratio(gross_return, favorable_excursion),
        }
    )


def _summary_row(
    candidate: Candidate,
    strategy: pd.DataFrame,
    trades: pd.DataFrame,
) -> dict[str, object]:
    metrics = _strategy_metrics(strategy)
    returns = trades["net_return_points"] if not trades.empty else pd.Series(dtype=float)
    winners = returns[returns > 0].sort_values(ascending=False)
    losers = returns[returns < 0].sort_values()
    total_return = float(returns.sum()) if not returns.empty else 0.0
    row = {
        **asdict(candidate),
        **metrics,
        "closed_trade_count": float(len(trades)),
        "profitable_year_count": _profitable_year_count(trades),
        "median_trade_points": float(returns.median()) if not returns.empty else 0.0,
        "mean_mfe_points": _mean_or_zero(trades, "mfe_points"),
        "mean_mae_points": _mean_or_zero(trades, "mae_points"),
        "median_mfe_points": _median_or_zero(trades, "mfe_points"),
        "median_mae_points": _median_or_zero(trades, "mae_points"),
        "mean_giveback_points": _mean_or_zero(trades, "giveback_from_mfe_points"),
        "median_holding_bars": _median_or_zero(trades, "holding_bars"),
        "mean_holding_bars": _mean_or_zero(trades, "holding_bars"),
        "top_1_win_points": float(winners.head(1).sum()) if not winners.empty else 0.0,
        "top_3_win_points": float(winners.head(3).sum()) if not winners.empty else 0.0,
        "top_5_win_points": float(winners.head(5).sum()) if not winners.empty else 0.0,
        "top_10_win_points": float(winners.head(10).sum()) if not winners.empty else 0.0,
        "top_5_win_return_share": _safe_ratio(float(winners.head(5).sum()), total_return),
        "without_top_1_win_points": total_return - float(winners.head(1).sum()),
        "without_top_3_win_points": total_return - float(winners.head(3).sum()),
        "without_top_5_win_points": total_return - float(winners.head(5).sum()),
        "worst_1_loss_points": float(losers.head(1).sum()) if not losers.empty else 0.0,
        "worst_3_loss_points": float(losers.head(3).sum()) if not losers.empty else 0.0,
        "worst_5_loss_points": float(losers.head(5).sum()) if not losers.empty else 0.0,
    }
    return row


def _yearly_rows(candidate: Candidate, trades: pd.DataFrame) -> list[dict[str, object]]:
    if trades.empty:
        return []
    result = trades.copy()
    result["year"] = _naive_datetimes(result["entry_time"]).dt.year
    rows = []
    for year, group in result.groupby("year", sort=True):
        returns = group["net_return_points"]
        rows.append(
            {
                **asdict(candidate),
                "year": int(year),
                "trade_count": float(len(group)),
                "total_return_points": float(returns.sum()),
                "win_rate": float((returns > 0).mean()),
                "profit_factor": _profit_factor(returns),
                "mean_trade_points": float(returns.mean()),
                "median_trade_points": float(returns.median()),
                "mean_mfe_points": float(group["mfe_points"].mean()),
                "mean_mae_points": float(group["mae_points"].mean()),
                "median_holding_bars": float(group["holding_bars"].median()),
            }
        )
    return rows


def _top_drawdown_rows(
    candidate: Candidate,
    strategy: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    top_n: int = 5,
) -> list[dict[str, object]]:
    equity = strategy["equity_points"].reset_index(drop=True)
    running_peak = equity.cummax()
    drawdown = running_peak - equity
    intervals: list[tuple[int, int]] = []
    rows: list[dict[str, object]] = []
    for trough in drawdown.sort_values(ascending=False).index:
        if drawdown.loc[trough] <= 0:
            break
        peak = int(equity.loc[:trough].idxmax())
        interval = (peak, int(trough))
        if any(not (interval[1] < used[0] or interval[0] > used[1]) for used in intervals):
            continue
        intervals.append(interval)
        overlapping_trades = trades[
            (trades["entry_row"] <= interval[1]) & (trades["exit_row"] >= interval[0])
        ]
        rows.append(
            {
                **asdict(candidate),
                "rank": len(rows) + 1,
                "peak_row": interval[0],
                "trough_row": interval[1],
                "peak_time": strategy.loc[interval[0], "datetime"],
                "trough_time": strategy.loc[interval[1], "datetime"],
                "peak_equity_points": float(equity.loc[interval[0]]),
                "trough_equity_points": float(equity.loc[interval[1]]),
                "drawdown_points": float(drawdown.loc[trough]),
                "duration_bars": interval[1] - interval[0],
                "duration_minutes": (interval[1] - interval[0]) * candidate.freq_minutes,
                "overlapping_trade_count": float(len(overlapping_trades)),
                "overlapping_trade_return_points": (
                    float(overlapping_trades["net_return_points"].sum())
                    if not overlapping_trades.empty
                    else 0.0
                ),
            }
        )
        if len(rows) >= top_n:
            break
    return rows


def _holding_bucket_rows(candidate: Candidate, trades: pd.DataFrame) -> list[dict[str, object]]:
    if trades.empty:
        return []
    result = trades.copy()
    result["holding_bucket"] = pd.cut(
        result["holding_bars"],
        bins=[0, 5, 10, 20, 50, 100, np.inf],
        labels=["1-5", "6-10", "11-20", "21-50", "51-100", "100+"],
        right=True,
        include_lowest=True,
    )
    rows = []
    for bucket, group in result.groupby("holding_bucket", observed=False):
        if group.empty:
            continue
        returns = group["net_return_points"]
        rows.append(
            {
                **asdict(candidate),
                "holding_bucket": str(bucket),
                "trade_count": float(len(group)),
                "total_return_points": float(returns.sum()),
                "mean_trade_points": float(returns.mean()),
                "win_rate": float((returns > 0).mean()),
                "profit_factor": _profit_factor(returns),
                "mean_mfe_points": float(group["mfe_points"].mean()),
                "mean_mae_points": float(group["mae_points"].mean()),
                "mean_giveback_points": float(group["giveback_from_mfe_points"].mean()),
            }
        )
    return rows


def _strategy_metrics(strategy: pd.DataFrame) -> dict[str, float]:
    returns = strategy["strategy_return_points"]
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    total_return = float(returns.sum())
    volatility = float(returns.std(ddof=0))
    sharpe = float(returns.mean() / volatility * np.sqrt(252)) if volatility else 0.0
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0
    return {
        "strategy_total_return_points": total_return,
        "strategy_max_drawdown_points": max_drawdown,
        "strategy_sharpe": sharpe,
        "strategy_calmar": float(total_return / max_drawdown) if max_drawdown else 0.0,
        "avg_abs_position": float(strategy["position"].abs().mean()),
    }


def _signal_hold_position(long_signal: pd.Series, short_signal: pd.Series) -> pd.Series:
    raw_position = pd.Series(np.nan, index=long_signal.index, dtype="float64")
    raw_position.loc[long_signal.fillna(False)] = 1.0
    raw_position.loc[short_signal.fillna(False)] = -1.0
    return raw_position.ffill().fillna(0.0)


def _profitable_year_count(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    result = trades.copy()
    result["year"] = _naive_datetimes(result["entry_time"]).dt.year
    yearly = result.groupby("year")["net_return_points"].sum()
    return float((yearly > 0).sum())


def _profit_factor(returns: pd.Series) -> float:
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if wins.empty or losses.empty:
        return 0.0
    return float(wins.sum() / abs(losses.sum()))


def _mean_or_zero(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].mean()) if not frame.empty and column in frame else 0.0


def _median_or_zero(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].median()) if not frame.empty and column in frame else 0.0


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _restrict_research_window(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["datetime"] = _naive_datetimes(result["datetime"])
    return result[result["datetime"] >= pd.Timestamp(RESEARCH_START)].reset_index(drop=True)


def _naive_datetimes(values: pd.Series) -> pd.Series:
    result = pd.to_datetime(values)
    if getattr(result.dt, "tz", None) is not None:
        result = result.dt.tz_localize(None)
    return result


if __name__ == "__main__":
    main()
