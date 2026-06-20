from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
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
BASELINE = "trend_macd_baseline"
STRICT_HARD = "strict_dc_hard_filter"
STICKY_HARD = "sticky_dc_hard_filter"
STICKY_WEIGHTED = "sticky_dc_weighted_bias"


@dataclass(frozen=True)
class TrendMacdParams:
    threshold_type: str
    atr_k: float
    atr_window: int


LOCKED_TREND_MACD_PARAMS = {
    5: TrendMacdParams("atr_ratio", 1.0, 50),
    15: TrendMacdParams("atr_ratio", 0.7, 50),
    30: TrendMacdParams("atr_ratio", 1.0, 100),
}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare strict/sticky DC trend structures on 5m, 15m, and 30m trend MACD."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--frequencies", type=int, nargs="+", default=[5, 15, 30])
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    continuous = load_continuous_1m(args.continuous_path)
    comparison_rows: list[dict[str, object]] = []
    yearly_rows: list[dict[str, object]] = []
    signal_rows: list[dict[str, object]] = []
    best_rows: list[dict[str, object]] = []

    for freq in args.frequencies:
        if freq not in LOCKED_TREND_MACD_PARAMS:
            raise ValueError(f"unsupported frequency: {freq}")
        bars = _restrict_research_window(aggregate_continuous_1m_to_Nm(continuous, freq))
        if bars.empty:
            continue

        params = LOCKED_TREND_MACD_PARAMS[freq]
        featured_base = add_trend_macd(
            bars,
            threshold_type=params.threshold_type,
            atr_k=params.atr_k,
            atr_window=params.atr_window,
            fast_period=12,
            slow_period=26,
            signal_period=9,
        )
        baseline_strategy = _evaluate_position(
            featured_base,
            _signal_hold_position(
                featured_base["trend_macd_golden_cross"],
                featured_base["trend_macd_death_cross"],
            ),
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        baseline_combo = _baseline_combo(freq)
        comparison_rows.extend(_period_rows(baseline_combo, BASELINE, baseline_strategy))
        yearly_rows.extend(_yearly_rows(baseline_combo, BASELINE, baseline_strategy))

        emitted_strict: set[tuple[float, float]] = set()
        pivoted_by_threshold = {
            threshold: add_dc_pivots(featured_base, threshold_points=threshold)
            for threshold in sorted({combo["dc_threshold_points"] for combo in _parameter_grid(freq)})
        }
        for combo in _parameter_grid(freq):
            pivoted = pivoted_by_threshold[combo["dc_threshold_points"]]
            strict_dc = _trend_state_from_confirmed_pivots(
                pivoted,
                mode="strict",
                entry_buffer_points=combo["trend_entry_buffer_points"],
                exit_buffer_points=combo["trend_entry_buffer_points"],
            )
            sticky_dc = _trend_state_from_confirmed_pivots(
                pivoted,
                mode="sticky",
                entry_buffer_points=combo["trend_entry_buffer_points"],
                exit_buffer_points=combo["trend_exit_buffer_points"],
            )
            featured = featured_base.copy()
            for prefix, dc_frame in [("strict_dc", strict_dc), ("sticky_dc", sticky_dc)]:
                featured[f"{prefix}_trend_state"] = dc_frame["dc_trend_state"]
                featured[f"{prefix}_is_bull"] = dc_frame["dc_is_bull"]
                featured[f"{prefix}_is_bear"] = dc_frame["dc_is_bear"]

            strategies = _strategy_results(
                featured,
                baseline_strategy=baseline_strategy,
                fee_points=args.fee_points,
                slippage_points=args.slippage_points,
            )
            signal_rows.append(_signal_stats_row(combo, featured, strategies))

            for strategy_name, strategy in strategies.items():
                if strategy_name == BASELINE:
                    continue
                output_combo = combo
                if strategy_name == STRICT_HARD:
                    strict_key = (
                        combo["freq_minutes"],
                        combo["dc_threshold_points"],
                        combo["trend_entry_buffer_points"],
                    )
                    if strict_key in emitted_strict:
                        continue
                    emitted_strict.add(strict_key)
                    output_combo = {**combo, "trend_exit_buffer_points": np.nan}
                comparison_rows.extend(_period_rows(output_combo, strategy_name, strategy))
                yearly_rows.extend(_yearly_rows(output_combo, strategy_name, strategy))

    comparison = pd.DataFrame(comparison_rows)
    yearly = pd.DataFrame(yearly_rows)
    signals = pd.DataFrame(signal_rows)
    best_rows = _best_rows(comparison)

    comparison.to_csv(
        args.reports_dir / "dc_structure_variants_comparison.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "dc_structure_variants_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    signals.to_csv(
        args.reports_dir / "dc_structure_variants_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(best_rows).to_csv(
        args.reports_dir / "dc_structure_variants_best_by_frequency.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (args.reports_dir / "dc_structure_variants_locked_trend_macd_params.json").write_text(
        json.dumps(
            {
                f"{freq}m": params.__dict__
                for freq, params in LOCKED_TREND_MACD_PARAMS.items()
                if freq in args.frequencies
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved reports to: {args.reports_dir}")


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
    peaks: list[float] = []
    valleys: list[float] = []
    current_state = "unknown"
    previous_row = 0

    pivot_rows = frame.index[frame["dc_pivot_kind"].notna()].to_list()
    for row_number in pivot_rows:
        states[previous_row:row_number] = current_state
        kind = frame.at[row_number, "dc_pivot_kind"]
        price = float(frame.at[row_number, "dc_pivot_price"])
        if kind == "peak":
            peaks.append(price)
        elif kind == "valley":
            valleys.append(price)
        else:
            raise ValueError(f"unsupported pivot kind: {kind}")

        if len(peaks) >= 2 and len(valleys) >= 2:
            current_state = _classify_pivot_structure(
                peaks[-2],
                peaks[-1],
                valleys[-2],
                valleys[-1],
                current_state=current_state,
                mode=mode,
                entry_buffer_points=entry_buffer_points,
                exit_buffer_points=exit_buffer_points,
            )
        states[row_number] = current_state
        previous_row = row_number + 1

    if previous_row < len(frame):
        states[previous_row:] = current_state

    result = frame.copy()
    result["dc_trend_state"] = states
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


def _strategy_results(
    featured: pd.DataFrame,
    *,
    baseline_strategy: pd.DataFrame,
    fee_points: float,
    slippage_points: float,
) -> dict[str, pd.DataFrame]:
    strict_position = _signal_hold_position(
        featured["trend_macd_golden_cross"] & featured["strict_dc_is_bull"],
        featured["trend_macd_death_cross"] & featured["strict_dc_is_bear"],
    )
    sticky_position = _signal_hold_position(
        featured["trend_macd_golden_cross"] & featured["sticky_dc_is_bull"],
        featured["trend_macd_death_cross"] & featured["sticky_dc_is_bear"],
    )
    weighted_position = _sticky_weighted_bias_position(featured)
    return {
        BASELINE: baseline_strategy,
        STRICT_HARD: _evaluate_position(
            featured,
            strict_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
        STICKY_HARD: _evaluate_position(
            featured,
            sticky_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
        STICKY_WEIGHTED: _evaluate_position(
            featured,
            weighted_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
    }


def _sticky_weighted_bias_position(featured: pd.DataFrame) -> pd.Series:
    base_direction = _signal_hold_position(
        featured["trend_macd_golden_cross"],
        featured["trend_macd_death_cross"],
    )
    long_weight, short_weight = _sticky_state_weights(featured["sticky_dc_trend_state"])
    position = pd.Series(0.0, index=featured.index, dtype="float64")
    position.loc[base_direction > 0] = long_weight.loc[base_direction > 0]
    position.loc[base_direction < 0] = -short_weight.loc[base_direction < 0]
    return position


def _sticky_state_weights(state: pd.Series) -> tuple[pd.Series, pd.Series]:
    long_weight = pd.Series(0.0, index=state.index, dtype="float64")
    short_weight = pd.Series(0.0, index=state.index, dtype="float64")
    bull = state == "bull"
    bear = state == "bear"
    sideways = state == "sideways"
    long_weight.loc[bull] = 1.0
    short_weight.loc[bull] = 0.3
    long_weight.loc[bear] = 0.3
    short_weight.loc[bear] = 1.0
    long_weight.loc[sideways] = 0.6
    short_weight.loc[sideways] = 0.6
    return long_weight, short_weight


def _signal_hold_position(long_signal: pd.Series, short_signal: pd.Series) -> pd.Series:
    raw_position = pd.Series(np.nan, index=long_signal.index, dtype="float64")
    raw_position.loc[long_signal.fillna(False)] = 1.0
    raw_position.loc[short_signal.fillna(False)] = -1.0
    return raw_position.ffill().fillna(0.0)


def _evaluate_position(
    frame: pd.DataFrame,
    position: pd.Series,
    *,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    result = frame[["datetime", "close"]].copy()
    result["position"] = position.astype("float64")
    result["trade"] = result["position"].diff().abs().fillna(result["position"].abs())
    result["bar_return_points"] = result["close"].diff().fillna(0.0)
    result["strategy_return_points"] = (
        result["position"].shift(1).fillna(0.0) * result["bar_return_points"]
        - result["trade"] * (fee_points + slippage_points)
    )
    result["equity_points"] = result["strategy_return_points"].cumsum()
    return result


def _parameter_grid(freq_minutes: int) -> list[dict[str, float]]:
    if freq_minutes == 5:
        thresholds = [10.0, 20.0, 30.0]
    elif freq_minutes == 15:
        thresholds = [15.0, 20.0, 30.0]
    else:
        thresholds = [20.0, 30.0, 40.0]

    rows: list[dict[str, float]] = []
    for threshold_points in thresholds:
        for entry_buffer in [5.0, 10.0, 15.0]:
            for exit_buffer in [15.0, 20.0, 30.0]:
                if exit_buffer < entry_buffer:
                    continue
                rows.append(
                    {
                        "freq_minutes": float(freq_minutes),
                        "trend_definition": _trend_definition_label(freq_minutes),
                        "dc_threshold_points": threshold_points,
                        "trend_entry_buffer_points": entry_buffer,
                        "trend_exit_buffer_points": exit_buffer,
                    }
                )
    return rows


def _baseline_combo(freq_minutes: int) -> dict[str, object]:
    return {
        "freq_minutes": float(freq_minutes),
        "trend_definition": _trend_definition_label(freq_minutes),
        "dc_threshold_points": np.nan,
        "trend_entry_buffer_points": np.nan,
        "trend_exit_buffer_points": np.nan,
    }


def _trend_definition_label(freq_minutes: int) -> str:
    params = LOCKED_TREND_MACD_PARAMS[freq_minutes]
    return f"atr_{params.atr_k}_N{params.atr_window}"


def _period_rows(
    combo: dict[str, object],
    strategy_name: str,
    strategy: pd.DataFrame,
) -> list[dict[str, object]]:
    return [
        {
            **combo,
            "period": period,
            "strategy": strategy_name,
            **_summary_metrics(strategy.loc[_period_mask(strategy, period)]),
        }
        for period in ["2018-2022", "2023-2025YTD", "full_2018-2025YTD"]
    ]


def _yearly_rows(
    combo: dict[str, object],
    strategy_name: str,
    strategy: pd.DataFrame,
) -> list[dict[str, object]]:
    frame = strategy.copy()
    frame["year"] = _naive_datetimes(frame["datetime"]).dt.year
    rows = []
    for year in sorted(frame["year"].dropna().unique()):
        rows.append(
            {
                **combo,
                "year": int(year),
                "strategy": strategy_name,
                **_summary_metrics(frame[frame["year"] == year]),
            }
        )
    return rows


def _period_mask(strategy: pd.DataFrame, period: str) -> pd.Series:
    datetimes = _naive_datetimes(strategy["datetime"])
    if period == "2018-2022":
        return (datetimes >= pd.Timestamp("2018-01-01")) & (
            datetimes <= pd.Timestamp("2022-12-31 23:59:59")
        )
    if period == "2023-2025YTD":
        return datetimes >= pd.Timestamp("2023-01-01")
    if period == "full_2018-2025YTD":
        return datetimes >= pd.Timestamp("2018-01-01")
    raise ValueError(f"unsupported period: {period}")


def _summary_metrics(strategy: pd.DataFrame) -> dict[str, float]:
    if strategy.empty:
        return _empty_metrics()
    returns = strategy["strategy_return_points"]
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    total_return = float(returns.sum())
    volatility = float(returns.std(ddof=0))
    sharpe = float(returns.mean() / volatility * np.sqrt(252)) if volatility else 0.0
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0
    calmar = float(total_return / max_drawdown) if max_drawdown else 0.0
    trades = _closed_directional_trade_returns(strategy)
    wins = [trade for trade in trades if trade > 0]
    losses = [trade for trade in trades if trade < 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    payoff = float(np.mean(wins) / abs(np.mean(losses))) if wins and losses else 0.0
    profit_factor = float(sum(wins) / abs(sum(losses))) if wins and losses else 0.0
    return {
        "total_return_points": total_return,
        "max_drawdown_points": max_drawdown,
        "sharpe": sharpe,
        "calmar": calmar,
        "trade_count": float(len(trades)),
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "profit_factor": profit_factor,
        "avg_abs_position": float(strategy["position"].abs().mean()),
    }


def _closed_directional_trade_returns(strategy: pd.DataFrame) -> list[float]:
    trades = []
    current = 0.0
    in_trade = False
    previous_sign = 0.0
    positions = strategy["position"].to_numpy(dtype=float, copy=False)
    returns = strategy["strategy_return_points"].to_numpy(dtype=float, copy=False)
    for position, strategy_return in zip(positions, returns):
        sign = float(np.sign(position))
        if not in_trade and sign != 0.0:
            in_trade = True
            current = 0.0
        if in_trade:
            current += float(strategy_return)
        if in_trade and previous_sign != 0.0 and sign != previous_sign:
            trades.append(current)
            current = 0.0
            in_trade = sign != 0.0
        previous_sign = sign
    return trades


def _empty_metrics() -> dict[str, float]:
    return {
        "total_return_points": 0.0,
        "max_drawdown_points": 0.0,
        "sharpe": 0.0,
        "calmar": 0.0,
        "trade_count": 0.0,
        "win_rate": 0.0,
        "payoff_ratio": 0.0,
        "profit_factor": 0.0,
        "avg_abs_position": 0.0,
    }


def _signal_stats_row(
    combo: dict[str, object],
    featured: pd.DataFrame,
    strategies: dict[str, pd.DataFrame],
) -> dict[str, object]:
    golden = featured["trend_macd_golden_cross"].fillna(False)
    death = featured["trend_macd_death_cross"].fillna(False)
    total_signals = int(golden.sum() + death.sum())
    strict_signals = int(
        (golden & featured["strict_dc_is_bull"]).sum()
        + (death & featured["strict_dc_is_bear"]).sum()
    )
    sticky_signals = int(
        (golden & featured["sticky_dc_is_bull"]).sum()
        + (death & featured["sticky_dc_is_bear"]).sum()
    )
    long_weight, short_weight = _sticky_state_weights(featured["sticky_dc_trend_state"])
    weighted_signals = int((golden & (long_weight > 0)).sum() + (death & (short_weight > 0)).sum())

    row: dict[str, object] = {
        **combo,
        "rows": float(len(featured)),
        "trend_macd_golden": int(golden.sum()),
        "trend_macd_death": int(death.sum()),
        "strict_dc_bull_ratio": float(featured["strict_dc_is_bull"].mean()),
        "strict_dc_bear_ratio": float(featured["strict_dc_is_bear"].mean()),
        "sticky_dc_bull_ratio": float(featured["sticky_dc_is_bull"].mean()),
        "sticky_dc_bear_ratio": float(featured["sticky_dc_is_bear"].mean()),
        "strict_hard_signal_throughput": _safe_ratio(strict_signals, total_signals),
        "sticky_hard_signal_throughput": _safe_ratio(sticky_signals, total_signals),
        "sticky_weighted_signal_throughput": _safe_ratio(weighted_signals, total_signals),
    }
    for strategy_name, strategy in strategies.items():
        row[f"{strategy_name}_avg_abs_position"] = float(strategy["position"].abs().mean())
    return row


def _best_rows(comparison: pd.DataFrame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    full = comparison[comparison["period"] == "full_2018-2025YTD"].copy()
    for freq in sorted(full["freq_minutes"].dropna().unique()):
        freq_rows = full[full["freq_minutes"] == freq].copy()
        for strategy in [BASELINE, STRICT_HARD, STICKY_HARD, STICKY_WEIGHTED]:
            subset = freq_rows[freq_rows["strategy"] == strategy].copy()
            if subset.empty:
                continue
            best = subset.sort_values(
                ["calmar", "sharpe", "total_return_points"],
                ascending=[False, False, False],
            ).iloc[0]
            rows.append(best.to_dict())
    return rows


def _safe_ratio(numerator: int, denominator: int) -> float:
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
