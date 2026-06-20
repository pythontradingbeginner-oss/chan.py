from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from data_foundation import (
    add_dc_structure,
    add_trend_macd,
    aggregate_continuous_1m_to_Nm,
    load_continuous_1m,
)


RESEARCH_START = "2018-01-01"
BASELINE_LABEL = "5m_baseline"
STRICT_HARD_LABEL = "strict_30m_hard_filter"
STICKY_HARD_LABEL = "sticky_30m_hard_filter"
STICKY_WEIGHTED_LABEL = "sticky_30m_weighted_bias"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run 5m trend MACD with strict/sticky 30m DC structure research."
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
    bars_5m = _restrict_research_window(aggregate_continuous_1m_to_Nm(continuous, 5))
    bars_30m = _restrict_research_window(aggregate_continuous_1m_to_Nm(continuous, 30))
    if bars_5m.empty or bars_30m.empty:
        raise ValueError("research window has no 5m or 30m bars")

    featured_5m = add_trend_macd(
        bars_5m,
        threshold_type="atr_ratio",
        atr_k=1.0,
        atr_window=50,
        fast_period=12,
        slow_period=26,
        signal_period=9,
    )
    baseline_position = _signal_hold_position(
        featured_5m["trend_macd_golden_cross"],
        featured_5m["trend_macd_death_cross"],
    )
    baseline_strategy = _evaluate_position(
        featured_5m,
        baseline_position,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )

    comparison_rows: list[dict[str, object]] = []
    yearly_rows: list[dict[str, object]] = []
    signal_rows: list[dict[str, object]] = []
    best_key: tuple[float, float, float] | None = None
    best_payload: dict[str, object] | None = None
    emitted_strict_keys: set[tuple[float, float]] = set()
    comparison_rows.extend(_period_rows(_baseline_combo(), BASELINE_LABEL, baseline_strategy))
    yearly_rows.extend(_yearly_rows(_baseline_combo(), BASELINE_LABEL, baseline_strategy))

    for combo in _parameter_grid():
        strict_30m = add_dc_structure(
            bars_30m,
            threshold_points=combo["dc_threshold_points"],
            trend_buffer_points=combo["trend_entry_buffer_points"],
            trend_mode="strict",
        )
        sticky_30m = add_dc_structure(
            bars_30m,
            threshold_points=combo["dc_threshold_points"],
            trend_buffer_points=combo["trend_entry_buffer_points"],
            trend_mode="sticky",
            trend_entry_buffer_points=combo["trend_entry_buffer_points"],
            trend_exit_buffer_points=combo["trend_exit_buffer_points"],
        )

        featured = map_higher_timeframe_dc_state(
            featured_5m,
            strict_30m,
            higher_freq_minutes=30,
            prefix="strict_dc30",
        )
        featured = map_higher_timeframe_dc_state(
            featured,
            sticky_30m,
            higher_freq_minutes=30,
            prefix="sticky_dc30",
        )

        strategies = _strategy_results(
            featured,
            baseline_strategy=baseline_strategy,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        for strategy_name, strategy in strategies.items():
            if strategy_name == BASELINE_LABEL:
                continue
            output_combo = combo
            if strategy_name == STRICT_HARD_LABEL:
                strict_key = (
                    combo["dc_threshold_points"],
                    combo["trend_entry_buffer_points"],
                )
                if strict_key in emitted_strict_keys:
                    continue
                emitted_strict_keys.add(strict_key)
                output_combo = {
                    **combo,
                    "trend_exit_buffer_points": np.nan,
                }
            comparison_rows.extend(_period_rows(output_combo, strategy_name, strategy))
            yearly_rows.extend(_yearly_rows(output_combo, strategy_name, strategy))

        signal_rows.append(_signal_stats_row(combo, featured, strategies))
        weighted_full = _summary_metrics(
            strategies[STICKY_WEIGHTED_LABEL].loc[
                _period_mask(strategies[STICKY_WEIGHTED_LABEL], "full_2018-2025YTD")
            ]
        )
        ranking_key = (
            weighted_full["calmar"],
            weighted_full["sharpe"],
            weighted_full["total_return_points"],
        )
        if best_key is None or ranking_key > best_key:
            best_key = ranking_key
            best_payload = {
                "combo": combo.copy(),
                "featured": featured,
                "strategies": strategies,
                "metrics": weighted_full,
            }

    pd.DataFrame(comparison_rows).to_csv(
        args.reports_dir / "trend_macd_5m_30m_dc_research_comparison.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(yearly_rows).to_csv(
        args.reports_dir / "trend_macd_5m_30m_dc_research_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(signal_rows).to_csv(
        args.reports_dir / "trend_macd_5m_30m_dc_research_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    if best_payload is not None:
        _write_best_outputs(best_payload, args.reports_dir)

    print(f"saved reports to: {args.reports_dir}")


def map_higher_timeframe_dc_state(
    lower_frame: pd.DataFrame,
    higher_dc: pd.DataFrame,
    *,
    higher_freq_minutes: int,
    datetime_col: str = "datetime",
    prefix: str = "dc30",
) -> pd.DataFrame:
    """Map completed higher-timeframe DC state onto lower-timeframe bars."""
    required = ["dc_trend_state", "dc_is_bull", "dc_is_bear"]
    missing_lower = [column for column in [datetime_col] if column not in lower_frame.columns]
    missing_higher = [column for column in [datetime_col, *required] if column not in higher_dc.columns]
    if missing_lower:
        raise ValueError(f"lower frame missing required column(s): {', '.join(missing_lower)}")
    if missing_higher:
        raise ValueError(f"higher frame missing required column(s): {', '.join(missing_higher)}")

    result = lower_frame.copy()
    result["__row_order"] = np.arange(len(result))
    result["__lower_time"] = _naive_datetimes(result[datetime_col])

    state_col = f"{prefix}_trend_state"
    bull_col = f"{prefix}_is_bull"
    bear_col = f"{prefix}_is_bear"
    available_col = f"{prefix}_available_at"

    if higher_dc.empty:
        result[state_col] = "unknown"
        result[bull_col] = False
        result[bear_col] = False
        result[available_col] = pd.NaT
        return result.drop(columns=["__row_order", "__lower_time"])

    higher = higher_dc[[datetime_col, *required]].copy()
    higher["__available_at"] = _naive_datetimes(higher[datetime_col]) + pd.Timedelta(
        minutes=higher_freq_minutes
    )
    higher = higher.rename(
        columns={
            "dc_trend_state": state_col,
            "dc_is_bull": bull_col,
            "dc_is_bear": bear_col,
            "__available_at": available_col,
        }
    )[[available_col, state_col, bull_col, bear_col]]

    merged = pd.merge_asof(
        result.sort_values("__lower_time"),
        higher.sort_values(available_col),
        left_on="__lower_time",
        right_on=available_col,
        direction="backward",
    )
    merged[state_col] = merged[state_col].fillna("unknown")
    merged[bull_col] = merged[bull_col].eq(True)
    merged[bear_col] = merged[bear_col].eq(True)
    return (
        merged.sort_values("__row_order")
        .drop(columns=["__row_order", "__lower_time"])
        .reset_index(drop=True)
    )


def _strategy_results(
    featured: pd.DataFrame,
    *,
    baseline_strategy: pd.DataFrame,
    fee_points: float,
    slippage_points: float,
) -> dict[str, pd.DataFrame]:
    strict_position = _signal_hold_position(
        featured["trend_macd_golden_cross"] & featured["strict_dc30_is_bull"],
        featured["trend_macd_death_cross"] & featured["strict_dc30_is_bear"],
    )
    sticky_position = _signal_hold_position(
        featured["trend_macd_golden_cross"] & featured["sticky_dc30_is_bull"],
        featured["trend_macd_death_cross"] & featured["sticky_dc30_is_bear"],
    )
    weighted_position = sticky_weighted_bias_position(featured)

    return {
        BASELINE_LABEL: baseline_strategy,
        STRICT_HARD_LABEL: _evaluate_position(
            featured,
            strict_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
        STICKY_HARD_LABEL: _evaluate_position(
            featured,
            sticky_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
        STICKY_WEIGHTED_LABEL: _evaluate_position(
            featured,
            weighted_position,
            fee_points=fee_points,
            slippage_points=slippage_points,
        ),
    }


def sticky_weighted_bias_position(featured: pd.DataFrame) -> pd.Series:
    base_direction = _signal_hold_position(
        featured["trend_macd_golden_cross"],
        featured["trend_macd_death_cross"],
    )
    long_weight, short_weight = _sticky_state_weights(featured["sticky_dc30_trend_state"])
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


def _parameter_grid() -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for threshold_points in [20, 30, 40]:
        for entry_buffer in [5, 10, 15]:
            for exit_buffer in [15, 20, 30]:
                if exit_buffer < entry_buffer:
                    continue
                rows.append(
                    {
                        "dc_threshold_points": float(threshold_points),
                        "trend_entry_buffer_points": float(entry_buffer),
                        "trend_exit_buffer_points": float(exit_buffer),
                    }
                )
    return rows


def _baseline_combo() -> dict[str, float]:
    return {
        "dc_threshold_points": np.nan,
        "trend_entry_buffer_points": np.nan,
        "trend_exit_buffer_points": np.nan,
    }


def _period_rows(
    combo: dict[str, float],
    strategy_name: str,
    strategy: pd.DataFrame,
) -> list[dict[str, object]]:
    rows = []
    for period in ["2018-2022", "2023-2025YTD", "full_2018-2025YTD"]:
        rows.append(
            {
                **combo,
                "period": period,
                "strategy": strategy_name,
                **_summary_metrics(strategy.loc[_period_mask(strategy, period)]),
            }
        )
    return rows


def _yearly_rows(
    combo: dict[str, float],
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
    combo: dict[str, float],
    featured: pd.DataFrame,
    strategies: dict[str, pd.DataFrame],
) -> dict[str, object]:
    golden = featured["trend_macd_golden_cross"].fillna(False)
    death = featured["trend_macd_death_cross"].fillna(False)
    total_signals = int(golden.sum() + death.sum())
    strict_signals = int(
        (golden & featured["strict_dc30_is_bull"]).sum()
        + (death & featured["strict_dc30_is_bear"]).sum()
    )
    sticky_signals = int(
        (golden & featured["sticky_dc30_is_bull"]).sum()
        + (death & featured["sticky_dc30_is_bear"]).sum()
    )
    long_weight, short_weight = _sticky_state_weights(featured["sticky_dc30_trend_state"])
    weighted_signals = int((golden & (long_weight > 0)).sum() + (death & (short_weight > 0)).sum())
    sticky_state = featured["sticky_dc30_trend_state"]

    row: dict[str, object] = {
        **combo,
        "rows": float(len(featured)),
        "trend_macd_golden": int(golden.sum()),
        "trend_macd_death": int(death.sum()),
        "strict_dc30_bull_ratio": float(featured["strict_dc30_is_bull"].mean()),
        "strict_dc30_bear_ratio": float(featured["strict_dc30_is_bear"].mean()),
        "sticky_dc30_bull_ratio": float(featured["sticky_dc30_is_bull"].mean()),
        "sticky_dc30_bear_ratio": float(featured["sticky_dc30_is_bear"].mean()),
        "strict_hard_signal_throughput": _safe_ratio(strict_signals, total_signals),
        "sticky_hard_signal_throughput": _safe_ratio(sticky_signals, total_signals),
        "sticky_weighted_signal_throughput": _safe_ratio(weighted_signals, total_signals),
        "weighted_long_favorable_signals": int((golden & (sticky_state == "bull")).sum()),
        "weighted_long_counter_signals": int((golden & (sticky_state == "bear")).sum()),
        "weighted_short_favorable_signals": int((death & (sticky_state == "bear")).sum()),
        "weighted_short_counter_signals": int((death & (sticky_state == "bull")).sum()),
    }
    for strategy_name, strategy in strategies.items():
        row[f"{strategy_name}_avg_abs_position"] = float(strategy["position"].abs().mean())
    return row


def _safe_ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _write_best_outputs(best_payload: dict[str, object], reports_dir: Path) -> None:
    combo = best_payload["combo"]
    featured = best_payload["featured"]
    strategies = best_payload["strategies"]
    metrics = best_payload["metrics"]
    assert isinstance(combo, dict)
    assert isinstance(featured, pd.DataFrame)
    assert isinstance(strategies, dict)
    assert isinstance(metrics, dict)

    equity = featured[
        [
            "datetime",
            "close",
            "strict_dc30_trend_state",
            "sticky_dc30_trend_state",
            "strict_dc30_available_at",
            "sticky_dc30_available_at",
        ]
    ].copy()
    for strategy_name, strategy in strategies.items():
        assert isinstance(strategy, pd.DataFrame)
        equity[f"{strategy_name}_position"] = strategy["position"].to_numpy()
        equity[f"{strategy_name}_equity_points"] = strategy["equity_points"].to_numpy()
    equity.to_csv(
        reports_dir / "trend_macd_5m_30m_dc_research_best_equity.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (reports_dir / "trend_macd_5m_30m_dc_research_best_parameters.json").write_text(
        json.dumps(
            {
                "selection": "best sticky_30m_weighted_bias by full-period calmar, sharpe, return",
                "parameters": combo,
                "metrics": metrics,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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
