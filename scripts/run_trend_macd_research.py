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
    add_trend_macd,
    aggregate_continuous_1m_to_Nm,
    build_continuous_contract,
    clean_rb_1m_bars,
    load_continuous_1m,
    load_raw_from_vnpy,
)


TRAIN_START = "2019-01-01"
TRAIN_END = "2021-12-31 23:59:59"
VALID_START = "2022-01-01"
VALID_END = "2022-12-31 23:59:59"
TEST_START = "2023-01-01"
TEST_END = "2024-12-31 23:59:59"


@dataclass(frozen=True)
class ThresholdCandidate:
    label: str
    threshold_type: str
    fixed_points: float | None = None
    atr_k: float | None = None
    atr_window: int | None = None
    quantile_p: float | None = None
    quantile_window: int | None = None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RB trend-bar MACD research.")
    parser.add_argument("--db-path", type=Path, default=Path(r"C:\Users\Administrator\.vntrader\database.db"))
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--frequencies", type=int, nargs="+", default=[5, 15, 30])
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.processed_dir.mkdir(parents=True, exist_ok=True)
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    continuous = _load_or_build_continuous(args.db_path, args.processed_dir)
    all_search_rows: list[dict[str, object]] = []
    all_baseline_rows: list[dict[str, object]] = []
    all_regime_rows: list[dict[str, object]] = []
    locked_parameters: dict[str, object] = {}

    for freq in args.frequencies:
        print(f"aggregating {freq}m bars...", flush=True)
        bars = aggregate_continuous_1m_to_Nm(continuous, freq)
        bars = _restrict_research_window(bars)
        if bars.empty:
            continue

        print(f"running parameter search for {freq}m ({len(bars)} bars)...", flush=True)
        search_rows, locked = _run_parameter_search(
            bars,
            freq_minutes=freq,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        all_search_rows.extend(search_rows)
        locked_parameters[f"{freq}m"] = asdict(locked)

        print(f"running locked evaluation for {freq}m with {locked.label}...", flush=True)
        baseline_rows, regime_rows = _run_locked_evaluation(
            bars,
            locked=locked,
            freq_minutes=freq,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        all_baseline_rows.extend(baseline_rows)
        all_regime_rows.extend(regime_rows)

    pd.DataFrame(all_search_rows).to_csv(
        args.reports_dir / "trend_macd_param_search.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_baseline_rows).to_csv(
        args.reports_dir / "trend_macd_baseline_comparison.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_regime_rows).to_csv(
        args.reports_dir / "trend_macd_regime_breakdown.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (args.reports_dir / "trend_macd_locked_parameters.json").write_text(
        json.dumps(
            {
                "locked_on": "validation",
                "train": [TRAIN_START, TRAIN_END],
                "validation": [VALID_START, VALID_END],
                "test": [TEST_START, TEST_END],
                "parameters": locked_parameters,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"saved reports to: {args.reports_dir}")


def _load_or_build_continuous(db_path: Path, processed_dir: Path) -> pd.DataFrame:
    continuous_path = processed_dir / "RB_1m_continuous.parquet"
    if continuous_path.exists():
        return load_continuous_1m(continuous_path)

    raw = load_raw_from_vnpy(db_path)
    cleaned, _ = clean_rb_1m_bars(raw)
    continuous, switch_log = build_continuous_contract(cleaned, method="backward_adjusted")

    cleaned.to_parquet(processed_dir / "RB_1m_raw_clean.parquet", compression="snappy", index=False)
    continuous.to_parquet(continuous_path, compression="snappy", index=False)
    switch_log.to_csv(processed_dir / "RB_main_switch_log.csv", index=False)
    return continuous


def _restrict_research_window(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["datetime"] = _naive_datetimes(result["datetime"])
    start = pd.Timestamp(TRAIN_START)
    end = pd.Timestamp(TEST_END)
    return result[(result["datetime"] >= start) & (result["datetime"] <= end)].reset_index(drop=True)


def _run_parameter_search(
    bars: pd.DataFrame,
    *,
    freq_minutes: int,
    fee_points: float,
    slippage_points: float,
) -> tuple[list[dict[str, object]], ThresholdCandidate]:
    rows = []
    candidates = _threshold_candidates(freq_minutes)
    for candidate in candidates:
        featured = _with_trend_macd(bars, candidate)
        metrics = _evaluate_strategy(
            featured,
            signal_prefix="trend",
            fee_points=fee_points,
            slippage_points=slippage_points,
        )
        for split_name, split_metrics in _split_metrics(featured, metrics).items():
            rows.append(
                {
                    "freq_minutes": freq_minutes,
                    "split": split_name,
                    "candidate": candidate.label,
                    **asdict(candidate),
                    **split_metrics,
                }
            )

    search = pd.DataFrame(rows)
    validation = search[search["split"] == "validation"].copy()
    validation = validation.sort_values(
        ["sharpe", "calmar", "total_return_points"],
        ascending=[False, False, False],
    )
    best_label = str(validation.iloc[0]["candidate"])
    locked = next(candidate for candidate in candidates if candidate.label == best_label)
    return rows, locked


def _run_locked_evaluation(
    bars: pd.DataFrame,
    *,
    locked: ThresholdCandidate,
    freq_minutes: int,
    fee_points: float,
    slippage_points: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    featured = _with_trend_macd(bars, locked)
    featured = _append_close_macd(featured)
    featured = _append_trend_regime(featured)

    strategies = {
        "traditional_macd": "close",
        "traditional_macd_trend_filter": "close_filtered",
        "trend_macd": "trend",
    }
    baseline_rows: list[dict[str, object]] = []
    regime_rows: list[dict[str, object]] = []

    for strategy_name, signal_prefix in strategies.items():
        metrics = _evaluate_strategy(
            featured,
            signal_prefix=signal_prefix,
            fee_points=fee_points,
            slippage_points=slippage_points,
        )
        for split_name, split_metrics in _split_metrics(featured, metrics).items():
            baseline_rows.append(
                {
                    "freq_minutes": freq_minutes,
                    "split": split_name,
                    "strategy": strategy_name,
                    "threshold_definition": _strategy_threshold_definition(
                        strategy_name,
                        locked,
                    ),
                    "threshold_usage": _strategy_threshold_usage(strategy_name),
                    **split_metrics,
                }
            )
        for regime_name, regime_metrics in _regime_metrics(featured, metrics).items():
            regime_rows.append(
                {
                    "freq_minutes": freq_minutes,
                    "strategy": strategy_name,
                    "regime": regime_name,
                    "threshold_definition": _strategy_threshold_definition(
                        strategy_name,
                        locked,
                    ),
                    "threshold_usage": _strategy_threshold_usage(strategy_name),
                    **regime_metrics,
                }
            )
    return baseline_rows, regime_rows


def _strategy_threshold_definition(
    strategy_name: str,
    locked: ThresholdCandidate,
) -> str:
    if strategy_name == "traditional_macd":
        return "none"
    return locked.label


def _strategy_threshold_usage(strategy_name: str) -> str:
    if strategy_name == "traditional_macd":
        return "none"
    if strategy_name == "traditional_macd_trend_filter":
        return "signal_filter"
    if strategy_name == "trend_macd":
        return "macd_calculation"
    return "unknown"


def _with_trend_macd(frame: pd.DataFrame, candidate: ThresholdCandidate) -> pd.DataFrame:
    return add_trend_macd(
        frame,
        threshold_type=candidate.threshold_type,
        fixed_points=candidate.fixed_points,
        atr_k=candidate.atr_k,
        atr_window=candidate.atr_window,
        quantile_p=candidate.quantile_p,
        quantile_window=candidate.quantile_window,
        fast_period=12,
        slow_period=26,
        signal_period=9,
    )


def _threshold_candidates(freq_minutes: int | None = None) -> list[ThresholdCandidate]:
    if freq_minutes == 30:
        return [
            *(
                ThresholdCandidate(f"fixed_{value}", "fixed", fixed_points=value)
                for value in [15, 18, 20, 25, 30, 35]
            ),
            *(
                ThresholdCandidate(f"atr_{k}_N{window}", "atr_ratio", atr_k=k, atr_window=window)
                for k in [0.6, 0.7, 0.8, 1.0, 1.2]
                for window in [20, 50, 100]
            ),
            *(
                ThresholdCandidate(
                    f"quantile_{p}_N{window}",
                    "quantile",
                    quantile_p=p,
                    quantile_window=window,
                )
                for p in [0.6, 0.7, 0.75, 0.8, 0.85]
                for window in [50, 100, 200]
            ),
        ]

    return [
        *(ThresholdCandidate(f"fixed_{value}", "fixed", fixed_points=value) for value in [5, 8, 10, 12, 15, 18, 20]),
        *(
            ThresholdCandidate(f"atr_{k}_N{window}", "atr_ratio", atr_k=k, atr_window=window)
            for k in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
            for window in [20, 50, 100]
        ),
        *(
            ThresholdCandidate(f"quantile_{p}_N{window}", "quantile", quantile_p=p, quantile_window=window)
            for p in [0.5, 0.6, 0.7, 0.75, 0.8]
            for window in [50, 100, 200]
        ),
    ]


def _append_close_macd(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    result["close_macd"] = macd
    result["close_macd_signal"] = signal
    result["close_macd_golden_cross"] = _crossed_above(macd, signal)
    result["close_macd_death_cross"] = _crossed_below(macd, signal)
    result["close_filtered_macd_golden_cross"] = result["close_macd_golden_cross"] & result["trend_is_bar"]
    result["close_filtered_macd_death_cross"] = result["close_macd_death_cross"] & result["trend_is_bar"]
    return result


def _evaluate_strategy(
    frame: pd.DataFrame,
    *,
    signal_prefix: str,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    result = frame[["datetime", "close"]].copy()
    if signal_prefix == "trend":
        golden = frame["trend_macd_golden_cross"]
        death = frame["trend_macd_death_cross"]
    else:
        golden = frame[f"{signal_prefix}_macd_golden_cross"]
        death = frame[f"{signal_prefix}_macd_death_cross"]

    raw_position = pd.Series(np.nan, index=frame.index)
    raw_position.loc[golden] = 1
    raw_position.loc[death] = -1
    result["position"] = raw_position.ffill().fillna(0)
    result["trade"] = result["position"].diff().abs().fillna(result["position"].abs())
    result["bar_return_points"] = result["close"].diff().fillna(0)
    result["strategy_return_points"] = (
        result["position"].shift(1).fillna(0) * result["bar_return_points"]
        - result["trade"] * (fee_points + slippage_points)
    )
    result["equity_points"] = result["strategy_return_points"].cumsum()
    result["trade_id"] = (result["trade"] > 0).cumsum()
    return result


def _split_metrics(frame: pd.DataFrame, strategy: pd.DataFrame) -> dict[str, dict[str, float]]:
    masks = {
        "train": _between(frame, TRAIN_START, TRAIN_END),
        "validation": _between(frame, VALID_START, VALID_END),
        "test": _between(frame, TEST_START, TEST_END),
    }
    return {name: _summary_metrics(strategy.loc[mask]) for name, mask in masks.items()}


def _regime_metrics(frame: pd.DataFrame, strategy: pd.DataFrame) -> dict[str, dict[str, float]]:
    metrics = {}
    for regime in ["trend", "sideways", "neutral"]:
        mask = frame["market_regime"] == regime
        metrics[regime] = _summary_metrics(strategy.loc[mask])
    return metrics


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
    trades = _closed_trade_returns(strategy)
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
    }


def _closed_trade_returns(strategy: pd.DataFrame) -> list[float]:
    trades = []
    current = 0.0
    in_trade = False
    previous_position = 0
    positions = strategy["position"].to_numpy(dtype=int, copy=False)
    returns = strategy["strategy_return_points"].to_numpy(dtype=float, copy=False)
    for position, strategy_return in zip(positions, returns):
        if previous_position == 0 and position != 0:
            in_trade = True
            current = 0.0
        if in_trade:
            current += float(strategy_return)
        if in_trade and previous_position != 0 and position != previous_position:
            trades.append(current)
            current = 0.0
            in_trade = position != 0
        previous_position = position
    return trades


def _append_trend_regime(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    adx = _adx(result, window=14)
    result["market_regime"] = "neutral"
    result.loc[adx > 25, "market_regime"] = "trend"
    result.loc[adx < 20, "market_regime"] = "sideways"
    return result


def _adx(frame: pd.DataFrame, *, window: int) -> pd.Series:
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(window, min_periods=window).mean()
    plus_di = 100 * plus_dm.rolling(window, min_periods=window).mean() / atr
    minus_di = 100 * minus_dm.rolling(window, min_periods=window).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di)) * 100
    return dx.rolling(window, min_periods=window).mean()


def _between(frame: pd.DataFrame, start: str, end: str) -> pd.Series:
    timestamps = _naive_datetimes(frame["datetime"])
    return (timestamps >= pd.Timestamp(start)) & (timestamps <= pd.Timestamp(end))


def _naive_datetimes(values: pd.Series) -> pd.Series:
    timestamps = pd.to_datetime(values)
    if getattr(timestamps.dt, "tz", None) is not None:
        return timestamps.dt.tz_localize(None)
    return timestamps


def _crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    return ((left > right) & (left.shift(1) <= right.shift(1))).fillna(False)


def _crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    return ((left < right) & (left.shift(1) >= right.shift(1))).fillna(False)


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
    }


if __name__ == "__main__":
    main()
