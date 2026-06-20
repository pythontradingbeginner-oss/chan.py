from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np
import pandas as pd

from Common.CEnum import KL_TYPE
from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from run_15m_sticky_exit_research import simulate_exit_strategy
from run_dc_structure_candidate_diagnostics import Candidate, _candidate_features
from run_rb_priority_strategy_research import (
    BASE_CANDIDATE,
    END,
    START,
    _chan_signals,
    _extract_trades,
    _naive_datetimes,
    _period_slices,
    _restrict_range,
)


BSP_TYPE_SETS = [
    ("1",),
    ("1p",),
    ("1", "1p"),
    ("2",),
    ("1", "1p", "2"),
    ("3a", "3b"),
    ("1", "1p", "2", "3a", "3b"),
]
DC_THRESHOLDS = [20.0, 30.0, 40.0]
OBV_WINDOWS = [20, 40, 60]
VOLUME_STRENGTHS = [1.0, 1.1, 1.2]
STRUCTURE_STOP_OPTIONS = [False, True]


@dataclass(frozen=True)
class ParamSet:
    bsp_types: tuple[str, ...]
    dc_threshold_points: float
    obv_window: int
    volume_strength: float
    structure_stop: bool

    @property
    def strategy_name(self) -> str:
        bsp = "-".join(self.bsp_types)
        stop = "stop" if self.structure_stop else "nostop"
        return (
            f"bsp_{bsp}_dc{int(self.dc_threshold_points)}_"
            f"obv{self.obv_window}_vol{self.volume_strength:g}_{stop}"
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Small parameter search around Chan BSP + DC + OBV/volume filter."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports/rb_chan_bsp_filter_param_search"),
    )
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--volume-window", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0, help="0 means full selected 15m bars.")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    continuous = _restrict_range(load_continuous_1m(args.continuous_path), args.start, args.end)
    bars_15m = _restrict_range(
        aggregate_continuous_1m_to_Nm(continuous, BASE_CANDIDATE.freq_minutes),
        args.start,
        args.end,
    )
    if args.limit > 0:
        bars_15m = bars_15m.iloc[: args.limit].reset_index(drop=True)

    chan_signals = _chan_signals(
        bars_15m,
        kl_type=KL_TYPE.K_15M,
        zs_breakout_buffer=5.0,
    )
    chan_signals.to_csv(
        args.reports_dir / "rb_15m_chan_signals.csv",
        index=False,
        encoding="utf-8-sig",
    )

    summary_rows: list[dict[str, object]] = []
    signal_rows: list[dict[str, object]] = []
    featured_cache: dict[tuple[float, int], pd.DataFrame] = {}

    for dc_threshold in DC_THRESHOLDS:
        candidate = Candidate(
            name=f"15m_sticky_dc{int(dc_threshold)}_entry15_exit30",
            freq_minutes=BASE_CANDIDATE.freq_minutes,
            trend_atr_k=BASE_CANDIDATE.trend_atr_k,
            trend_atr_window=BASE_CANDIDATE.trend_atr_window,
            dc_threshold_points=dc_threshold,
            dc_mode=BASE_CANDIDATE.dc_mode,
            dc_entry_buffer_points=BASE_CANDIDATE.dc_entry_buffer_points,
            dc_exit_buffer_points=BASE_CANDIDATE.dc_exit_buffer_points,
        )
        featured_base = _candidate_features(bars_15m, candidate)

        for obv_window in OBV_WINDOWS:
            featured = _append_obv_volume_windows(
                featured_base,
                obv_window=obv_window,
                volume_window=args.volume_window,
            )
            featured_cache[(dc_threshold, obv_window)] = featured
            for volume_strength in VOLUME_STRENGTHS:
                for bsp_types in BSP_TYPE_SETS:
                    for structure_stop in STRUCTURE_STOP_OPTIONS:
                        params = ParamSet(
                            bsp_types=bsp_types,
                            dc_threshold_points=dc_threshold,
                            obv_window=obv_window,
                            volume_strength=volume_strength,
                            structure_stop=structure_stop,
                        )
                        base_long, base_short = _signals_for_params(
                            featured,
                            chan_signals,
                            params,
                        )
                        summary_rows.extend(
                            _fast_summary_rows_for_params(
                                params,
                                featured,
                                base_long,
                                base_short,
                                fee_points=args.fee_points,
                                slippage_points=args.slippage_points,
                            )
                        )
                        signal_rows.append(
                            {
                                "strategy": params.strategy_name,
                                "bsp_types": ",".join(params.bsp_types),
                                "dc_threshold_points": params.dc_threshold_points,
                                "obv_window": params.obv_window,
                                "volume_strength": params.volume_strength,
                                "structure_stop": params.structure_stop,
                                "long_signals": int(base_long.sum()),
                                "short_signals": int(base_short.sum()),
                                "total_signals": int(base_long.sum() + base_short.sum()),
                            }
                        )

    summary = pd.DataFrame(summary_rows)
    signal_stats = pd.DataFrame(signal_rows)
    ranking = _rank_param_sets(summary)

    summary.to_csv(
        args.reports_dir / "param_search_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    ranking.to_csv(
        args.reports_dir / "param_search_ranking.csv",
        index=False,
        encoding="utf-8-sig",
    )
    signal_stats.to_csv(
        args.reports_dir / "param_search_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )

    top_trade_frames: list[pd.DataFrame] = []
    top_value_frames: list[pd.DataFrame] = []
    for _, row in ranking.head(10).iterrows():
        params = _params_from_rank_row(row)
        featured = featured_cache[(params.dc_threshold_points, params.obv_window)]
        long_signal, short_signal = _signals_for_params(featured, chan_signals, params)
        strategy = simulate_exit_strategy(
                            featured,
            long_signal=long_signal,
            short_signal=short_signal,
            strategy_name=params.strategy_name,
            reverse_mode="reverse",
            trail_trigger_points=None,
            giveback_ratio=None,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
            enable_structure_stop=params.structure_stop,
        )
        trades = _extract_trades(featured, strategy)
        top_value_frames.append(strategy)
        top_trade_frames.append(trades)
    if top_value_frames:
        pd.concat(top_value_frames, ignore_index=True).to_csv(
            args.reports_dir / "top10_strategy_values.csv",
            index=False,
            encoding="utf-8-sig",
        )
    if top_trade_frames:
        pd.concat(top_trade_frames, ignore_index=True).to_csv(
            args.reports_dir / "top10_strategy_trades.csv",
            index=False,
            encoding="utf-8-sig",
        )

    print(f"saved reports to: {args.reports_dir}")
    print(ranking.head(15).to_string(index=False))


def _append_obv_volume_windows(
    frame: pd.DataFrame,
    *,
    obv_window: int,
    volume_window: int,
) -> pd.DataFrame:
    result = frame.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    volume = pd.to_numeric(result["volume"], errors="coerce").fillna(0.0)
    direction = np.sign(close.diff()).fillna(0.0)
    result["obv"] = (direction * volume).cumsum()
    result["obv_ma"] = result["obv"].rolling(obv_window, min_periods=obv_window).mean()
    result["volume_ma"] = volume.rolling(volume_window, min_periods=volume_window).mean()
    result["obv_above_ma"] = result["obv"] > result["obv_ma"]
    result["obv_below_ma"] = result["obv"] < result["obv_ma"]
    return result


def _signals_for_params(
    featured: pd.DataFrame,
    chan_signals: pd.DataFrame,
    params: ParamSet,
) -> tuple[pd.Series, pd.Series]:
    type_mask = _bsp_type_mask(chan_signals["chan_bsp_type"], params.bsp_types)
    obv_volume_long = (
        featured["obv_above_ma"]
        & (featured["volume"] > featured["volume_ma"] * params.volume_strength)
    )
    obv_volume_short = (
        featured["obv_below_ma"]
        & (featured["volume"] > featured["volume_ma"] * params.volume_strength)
    )
    long_signal = (
        chan_signals["chan_bsp_buy"]
        & type_mask
        & featured["dc_is_bull"]
        & obv_volume_long
    )
    short_signal = (
        chan_signals["chan_bsp_sell"]
        & type_mask
        & featured["dc_is_bear"]
        & obv_volume_short
    )
    return long_signal, short_signal


def _bsp_type_mask(series: pd.Series, allowed: tuple[str, ...]) -> pd.Series:
    allowed_set = set(allowed)
    return series.fillna("").map(
        lambda value: bool(allowed_set.intersection(str(value).split(",")))
    )


def _fast_summary_rows_for_params(
    params: ParamSet,
    featured: pd.DataFrame,
    long_signal: pd.Series,
    short_signal: pd.Series,
    *,
    fee_points: float,
    slippage_points: float,
) -> list[dict[str, object]]:
    positions, returns, trade_entries, trade_returns = _fast_backtest_arrays(
        featured,
        long_signal,
        short_signal,
        structure_stop=params.structure_stop,
        fee_points=fee_points,
        slippage_points=slippage_points,
    )
    datetimes = _naive_datetimes(featured["datetime"])
    train_mask = (datetimes <= pd.Timestamp("2022-12-31 23:59:59")).to_numpy()
    test_mask = (datetimes >= pd.Timestamp("2023-01-01 00:00:00")).to_numpy()
    rows = []
    for period, mask in [
        ("train_2018_2022", train_mask),
        ("test_2023_2025YTD", test_mask),
        ("full_2018_2025YTD", np.ones(len(featured), dtype=bool)),
    ]:
        rows.append(
            {
                "strategy": params.strategy_name,
                "period": period,
                "bsp_types": ",".join(params.bsp_types),
                "dc_threshold_points": params.dc_threshold_points,
                "obv_window": params.obv_window,
                "volume_strength": params.volume_strength,
                "structure_stop": params.structure_stop,
                **_metrics_from_arrays(
                    positions[mask],
                    returns[mask],
                    _trade_returns_for_mask(trade_entries, trade_returns, mask),
                ),
            }
        )
    return rows


def _fast_backtest_arrays(
    featured: pd.DataFrame,
    long_signal: pd.Series,
    short_signal: pd.Series,
    *,
    structure_stop: bool,
    fee_points: float,
    slippage_points: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    closes = pd.to_numeric(featured["close"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(featured["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(featured["low"], errors="coerce").to_numpy(dtype=float)
    long_values = long_signal.fillna(False).to_numpy(dtype=bool)
    short_values = short_signal.fillna(False).to_numpy(dtype=bool)
    valley_stops = pd.to_numeric(
        featured.get("dc_recent_valley_price"),
        errors="coerce",
    ).to_numpy(dtype=float)
    peak_stops = pd.to_numeric(
        featured.get("dc_recent_peak_price"),
        errors="coerce",
    ).to_numpy(dtype=float)
    positions = np.zeros(len(featured), dtype=float)
    returns = np.zeros(len(featured), dtype=float)
    trade_entries: list[int] = []
    trade_returns: list[float] = []

    cost_per_trade = fee_points + slippage_points
    position = 0.0
    stop_price = np.nan
    current_trade: float | None = None
    current_entry: int | None = None
    previous_sign = 0

    for index, close in enumerate(closes):
        previous_close = closes[index - 1] if index > 0 else close
        start_position = position
        event_price = close
        signal_used = False

        if long_values[index]:
            signal_used = True
            if position != 1.0:
                position = 1.0
                stop_price = _structure_stop_price(
                    side=1,
                    entry_price=close,
                    source_price=valley_stops[index],
                )
        elif short_values[index]:
            signal_used = True
            if position != -1.0:
                position = -1.0
                stop_price = _structure_stop_price(
                    side=-1,
                    entry_price=close,
                    source_price=peak_stops[index],
                )

        if (
            structure_stop
            and not signal_used
            and start_position != 0.0
            and not np.isnan(stop_price)
        ):
            if start_position > 0 and lows[index] <= stop_price:
                position = 0.0
                event_price = stop_price
                stop_price = np.nan
            elif start_position < 0 and highs[index] >= stop_price:
                position = 0.0
                event_price = stop_price
                stop_price = np.nan

        if position == 0.0:
            stop_price = np.nan

        trade_size = abs(position - start_position)
        strategy_return = (
            start_position * (event_price - previous_close) - trade_size * cost_per_trade
        )
        positions[index] = position
        returns[index] = strategy_return

        sign = int(np.sign(position))
        if current_trade is None and sign != 0:
            current_trade = 0.0
            current_entry = index
        if current_trade is not None:
            current_trade += float(strategy_return)
        if current_trade is not None and previous_sign != 0 and sign != previous_sign:
            trade_entries.append(current_entry if current_entry is not None else index)
            trade_returns.append(current_trade)
            current_trade = 0.0 if sign != 0 else None
            current_entry = index if sign != 0 else None
        previous_sign = sign

    if current_trade is not None:
        trade_entries.append(current_entry if current_entry is not None else len(featured) - 1)
        trade_returns.append(current_trade)

    return (
        positions,
        returns,
        np.asarray(trade_entries, dtype=int),
        np.asarray(trade_returns, dtype=float),
    )


def _structure_stop_price(side: int, entry_price: float, source_price: float) -> float:
    if np.isnan(source_price):
        return np.nan
    if side > 0:
        return float(source_price if source_price <= entry_price else entry_price - abs(entry_price - source_price))
    return float(source_price if source_price >= entry_price else entry_price + abs(entry_price - source_price))


def _trade_returns_for_mask(
    trade_entries: np.ndarray,
    trade_returns: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    if len(trade_entries) == 0:
        return np.asarray([], dtype=float)
    return trade_returns[mask[trade_entries]]


def _metrics_from_arrays(
    positions: np.ndarray,
    returns: np.ndarray,
    trade_returns: np.ndarray,
) -> dict[str, float]:
    if len(returns) == 0:
        return _empty_metrics()
    equity = np.cumsum(returns)
    drawdown = np.maximum.accumulate(equity) - equity
    wins = trade_returns[trade_returns > 0]
    losses = trade_returns[trade_returns < 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(losses.sum()) if len(losses) else 0.0
    max_drawdown = float(drawdown.max()) if len(drawdown) else 0.0
    total_return = float(returns.sum())
    return {
        "bar_count": float(len(returns)),
        "trade_count": float(len(trade_returns)),
        "total_return_points": total_return,
        "max_drawdown_points": max_drawdown,
        "return_drawdown_ratio": total_return / max_drawdown if max_drawdown else 0.0,
        "win_rate": float(len(wins) / len(trade_returns)) if len(trade_returns) else 0.0,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss else 0.0,
        "expectancy_points": float(trade_returns.mean()) if len(trade_returns) else 0.0,
        "avg_abs_position": float(np.mean(np.abs(positions))) if len(positions) else 0.0,
        "largest_win_points": float(trade_returns.max()) if len(trade_returns) else 0.0,
        "largest_loss_points": float(trade_returns.min()) if len(trade_returns) else 0.0,
    }


def _summary_rows_for_params(
    params: ParamSet,
    strategy: pd.DataFrame,
) -> list[dict[str, object]]:
    rows = []
    for period, period_strategy in _period_slices(strategy):
        rows.append(
            {
                "strategy": params.strategy_name,
                "period": period,
                "bsp_types": ",".join(params.bsp_types),
                "dc_threshold_points": params.dc_threshold_points,
                "obv_window": params.obv_window,
                "volume_strength": params.volume_strength,
                "structure_stop": params.structure_stop,
                **_strategy_metrics_from_strategy(period_strategy),
            }
        )
    return rows


def _strategy_metrics_from_strategy(strategy: pd.DataFrame) -> dict[str, float]:
    if strategy.empty:
        return _empty_metrics()
    returns = pd.to_numeric(strategy["strategy_return_points"], errors="coerce").fillna(0.0)
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    trade_returns = pd.Series(_closed_trade_returns(strategy), dtype=float)
    wins = trade_returns[trade_returns > 0]
    losses = trade_returns[trade_returns < 0]
    gross_profit = float(wins.sum()) if not wins.empty else 0.0
    gross_loss = float(losses.sum()) if not losses.empty else 0.0
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0
    total_return = float(returns.sum())
    return {
        "bar_count": float(len(strategy)),
        "trade_count": float(len(trade_returns)),
        "total_return_points": total_return,
        "max_drawdown_points": max_drawdown,
        "return_drawdown_ratio": total_return / max_drawdown if max_drawdown else 0.0,
        "win_rate": float(len(wins) / len(trade_returns)) if len(trade_returns) else 0.0,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss else 0.0,
        "expectancy_points": float(trade_returns.mean()) if len(trade_returns) else 0.0,
        "avg_abs_position": float(strategy["position"].abs().mean()),
        "largest_win_points": float(trade_returns.max()) if len(trade_returns) else 0.0,
        "largest_loss_points": float(trade_returns.min()) if len(trade_returns) else 0.0,
    }


def _closed_trade_returns(strategy: pd.DataFrame) -> list[float]:
    trades: list[float] = []
    current: float | None = None
    previous_sign = 0
    positions = strategy["position"].to_numpy(dtype=float, copy=False)
    returns = strategy["strategy_return_points"].to_numpy(dtype=float, copy=False)
    for position, strategy_return in zip(positions, returns):
        sign = int(np.sign(position))
        if current is None and sign != 0:
            current = 0.0
        if current is not None:
            current += float(strategy_return)
        if current is not None and previous_sign != 0 and sign != previous_sign:
            trades.append(current)
            current = 0.0 if sign != 0 else None
        previous_sign = sign
    if current is not None:
        trades.append(current)
    return trades


def _empty_metrics() -> dict[str, float]:
    return {
        "bar_count": 0.0,
        "trade_count": 0.0,
        "total_return_points": 0.0,
        "max_drawdown_points": 0.0,
        "return_drawdown_ratio": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "expectancy_points": 0.0,
        "avg_abs_position": 0.0,
        "largest_win_points": 0.0,
        "largest_loss_points": 0.0,
    }


def _params_from_rank_row(row: pd.Series) -> ParamSet:
    return ParamSet(
        bsp_types=tuple(str(row["bsp_types"]).split(",")),
        dc_threshold_points=float(row["dc_threshold_points"]),
        obv_window=int(row["obv_window"]),
        volume_strength=float(row["volume_strength"]),
        structure_stop=bool(row["structure_stop"]),
    )


def _rank_param_sets(summary: pd.DataFrame) -> pd.DataFrame:
    train = summary[summary["period"] == "train_2018_2022"].copy()
    test = summary[summary["period"] == "test_2023_2025YTD"].copy()
    merged = train.merge(
        test,
        on=[
            "strategy",
            "bsp_types",
            "dc_threshold_points",
            "obv_window",
            "volume_strength",
            "structure_stop",
        ],
        suffixes=("_train", "_test"),
    )
    merged["robust_score"] = (
        merged["return_drawdown_ratio_test"].clip(lower=-2, upper=5)
        + 0.4 * merged["return_drawdown_ratio_train"].clip(lower=-2, upper=5)
        + 0.001 * merged["total_return_points_test"]
        - 0.001 * merged["max_drawdown_points_test"]
    )
    tradable = merged[
        (merged["trade_count_train"] >= 8)
        & (merged["trade_count_test"] >= 3)
        & (merged["total_return_points_train"] > 0)
    ].copy()
    source = tradable if not tradable.empty else merged
    return source.sort_values(
        [
            "robust_score",
            "return_drawdown_ratio_test",
            "total_return_points_test",
            "max_drawdown_points_test",
        ],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)


if __name__ == "__main__":
    main()
