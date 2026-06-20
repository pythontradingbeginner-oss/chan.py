from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
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
from run_dc_structure_candidate_diagnostics import Candidate, _candidate_features
from run_rb_chan_bsp_filter_param_search import (
    ParamSet,
    _append_obv_volume_windows,
    _signals_for_params,
)
from run_rb_priority_strategy_research import (
    BASE_CANDIDATE,
    END,
    START,
    TEST_START,
    TRAIN_END,
    _chan_signals,
    _naive_datetimes,
    _restrict_range,
)


MAIN_PARAMS = ParamSet(
    bsp_types=("1", "1p", "2"),
    dc_threshold_points=30.0,
    obv_window=40,
    volume_strength=1.0,
    structure_stop=False,
)
VOLUME_WINDOW = 20


@dataclass(frozen=True)
class ExitConfig:
    name: str
    opposite_mode: str = "reverse"
    structure_stop: bool = False
    fixed_stop_points: float | None = None
    trail_trigger_points: float | None = None
    giveback_ratio: float | None = None
    max_hold_bars: int | None = None


MAIN_EXIT_CONFIG = ExitConfig(
    name="trail_500_gb50",
    trail_trigger_points=500.0,
    giveback_ratio=0.5,
)
MAIN_STRATEGY_NAME = f"{MAIN_PARAMS.strategy_name}_{MAIN_EXIT_CONFIG.name}"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research exit rules for the fixed RB Chan BSP + DC30 + OBV40 candidate."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports/rb_main_candidate_exit_research"),
    )
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument(
        "--chan-signals-path",
        type=Path,
        default=None,
        help="Optional existing rb_15m_chan_signals.csv from the same date range.",
    )
    parser.add_argument("--zs-breakout-buffer", type=float, default=5.0)
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

    candidate = Candidate(
        name="main_bsp_1-1p-2_dc30_obv40_vol1",
        freq_minutes=BASE_CANDIDATE.freq_minutes,
        trend_atr_k=BASE_CANDIDATE.trend_atr_k,
        trend_atr_window=BASE_CANDIDATE.trend_atr_window,
        dc_threshold_points=MAIN_PARAMS.dc_threshold_points,
        dc_mode=BASE_CANDIDATE.dc_mode,
        dc_entry_buffer_points=BASE_CANDIDATE.dc_entry_buffer_points,
        dc_exit_buffer_points=BASE_CANDIDATE.dc_exit_buffer_points,
    )
    featured = _append_obv_volume_windows(
        _candidate_features(bars_15m, candidate),
        obv_window=MAIN_PARAMS.obv_window,
        volume_window=VOLUME_WINDOW,
    )

    chan_signals = _load_or_build_chan_signals(
        args,
        featured,
        args.reports_dir / "rb_15m_chan_signals.csv",
    )
    long_signal, short_signal = _signals_for_params(featured, chan_signals, MAIN_PARAMS)
    signal_stats = _signal_stats(featured, chan_signals, long_signal, short_signal)

    strategies: list[pd.DataFrame] = []
    trades: list[pd.DataFrame] = []
    configs = _exit_configs()
    for config in configs:
        strategy = simulate_exit_rule(
            featured,
            long_signal=long_signal,
            short_signal=short_signal,
            strategy_name=f"{MAIN_PARAMS.strategy_name}_{config.name}",
            config=config,
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        strategies.append(strategy)
        trades.append(_extract_trades(featured, strategy))

    strategy_report = pd.concat(strategies, ignore_index=True)
    trade_report = pd.concat(trades, ignore_index=True)
    summary = _summary_rows(strategy_report, trade_report)
    ranking = _rank_exit_configs(summary)
    yearly = _yearly_rows(strategy_report, trade_report)
    drawdowns = _top_drawdown_rows(strategy_report, trade_report)

    featured.to_csv(args.reports_dir / "rb_15m_main_candidate_features.csv", index=False, encoding="utf-8-sig")
    strategy_report.to_csv(args.reports_dir / "exit_rule_values.csv", index=False, encoding="utf-8-sig")
    trade_report.to_csv(args.reports_dir / "exit_rule_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(args.reports_dir / "exit_rule_summary.csv", index=False, encoding="utf-8-sig")
    ranking.to_csv(args.reports_dir / "exit_rule_ranking.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(args.reports_dir / "exit_rule_yearly.csv", index=False, encoding="utf-8-sig")
    drawdowns.to_csv(args.reports_dir / "exit_rule_drawdowns.csv", index=False, encoding="utf-8-sig")
    signal_stats.to_csv(args.reports_dir / "main_candidate_signal_stats.csv", index=False, encoding="utf-8-sig")
    (args.reports_dir / "exit_rule_configs.json").write_text(
        json.dumps([asdict(config) for config in configs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"saved reports to: {args.reports_dir}")
    print(ranking.head(15).to_string(index=False))


def _load_or_build_chan_signals(
    args: argparse.Namespace,
    featured: pd.DataFrame,
    output_path: Path,
) -> pd.DataFrame:
    if args.chan_signals_path is not None and args.chan_signals_path.exists():
        chan_signals = pd.read_csv(args.chan_signals_path)
        if len(chan_signals) != len(featured):
            raise ValueError(
                "chan signals row count does not match featured bars: "
                f"{len(chan_signals)} != {len(featured)}"
            )
        chan_signals["datetime"] = featured["datetime"].to_numpy()
    else:
        chan_signals = _chan_signals(
            featured,
            kl_type=KL_TYPE.K_15M,
            zs_breakout_buffer=args.zs_breakout_buffer,
        )
    chan_signals.to_csv(output_path, index=False, encoding="utf-8-sig")
    return chan_signals


def _exit_configs() -> list[ExitConfig]:
    configs: list[ExitConfig] = [
        ExitConfig(name="reverse_only", opposite_mode="reverse"),
        ExitConfig(name="flat_on_opposite", opposite_mode="flat"),
        ExitConfig(name="structure_stop", structure_stop=True),
    ]
    for stop_points in [120.0, 180.0, 240.0, 320.0]:
        configs.append(
            ExitConfig(
                name=f"fixed_stop_{int(stop_points)}",
                fixed_stop_points=stop_points,
            )
        )
    for trigger in [300.0, 500.0, 800.0]:
        for giveback in [0.3, 0.5]:
            configs.append(
                ExitConfig(
                    name=f"trail_{int(trigger)}_gb{int(giveback * 100)}",
                    trail_trigger_points=trigger,
                    giveback_ratio=giveback,
                )
            )
    for stop_points, trigger, giveback in [
        (180.0, 300.0, 0.5),
        (180.0, 500.0, 0.5),
        (240.0, 500.0, 0.5),
        (240.0, 800.0, 0.5),
    ]:
        configs.append(
            ExitConfig(
                name=f"fixed_{int(stop_points)}_trail_{int(trigger)}_gb{int(giveback * 100)}",
                fixed_stop_points=stop_points,
                trail_trigger_points=trigger,
                giveback_ratio=giveback,
            )
        )
    for trigger in [300.0, 500.0, 800.0]:
        configs.append(
            ExitConfig(
                name=f"structure_trail_{int(trigger)}_gb50",
                structure_stop=True,
                trail_trigger_points=trigger,
                giveback_ratio=0.5,
            )
        )
    for hold_bars in [192, 384]:
        configs.append(
            ExitConfig(
                name=f"time_stop_{hold_bars}bars",
                max_hold_bars=hold_bars,
            )
        )
    return configs


def simulate_exit_rule(
    featured: pd.DataFrame,
    *,
    long_signal: pd.Series,
    short_signal: pd.Series,
    strategy_name: str,
    config: ExitConfig,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    if config.opposite_mode not in {"reverse", "flat"}:
        raise ValueError("opposite_mode must be one of: reverse, flat")
    if config.fixed_stop_points is not None and config.fixed_stop_points <= 0:
        raise ValueError("fixed_stop_points must be positive")
    if config.trail_trigger_points is not None and config.trail_trigger_points <= 0:
        raise ValueError("trail_trigger_points must be positive")
    if config.giveback_ratio is not None and not 0 < config.giveback_ratio < 1:
        raise ValueError("giveback_ratio must be between 0 and 1")
    if (config.trail_trigger_points is None) != (config.giveback_ratio is None):
        raise ValueError("trail_trigger_points and giveback_ratio must be set together")
    if config.max_hold_bars is not None and config.max_hold_bars <= 0:
        raise ValueError("max_hold_bars must be positive")

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

    positions: list[float] = []
    event_reasons: list[str] = []
    event_prices: list[float] = []
    active_stop_prices: list[float] = []
    structure_stop_prices: list[float] = []
    fixed_stop_prices: list[float] = []
    trail_mfe_values: list[float] = []
    trail_giveback_values: list[float] = []
    strategy_returns: list[float] = []
    bars_held_values: list[float] = []

    cost_per_trade = fee_points + slippage_points
    position = 0.0
    entry_price = np.nan
    entry_row: int | None = None
    structure_stop_price = np.nan
    fixed_stop_price = np.nan
    best_close_move = 0.0

    for index, close in enumerate(closes):
        previous_close = closes[index - 1] if index > 0 else close
        start_position = position
        event_price = close
        reason = "hold"
        giveback = 0.0

        if start_position != 0.0 and not np.isnan(entry_price):
            current_move = start_position * (close - entry_price)
            best_close_move = max(best_close_move, current_move)
            giveback = best_close_move - current_move

        exited = False
        if start_position != 0.0 and entry_row is not None and index > entry_row:
            stop_reason, stop_price = _first_triggered_stop(
                side=int(start_position),
                low=lows[index],
                high=highs[index],
                structure_stop_price=structure_stop_price,
                fixed_stop_price=fixed_stop_price,
            )
            if stop_reason is not None:
                position = 0.0
                event_price = stop_price
                reason = stop_reason
                exited = True
            elif (
                config.trail_trigger_points is not None
                and config.giveback_ratio is not None
                and best_close_move >= config.trail_trigger_points
                and giveback >= best_close_move * config.giveback_ratio
            ):
                position = 0.0
                reason = "trail_exit"
                exited = True
            elif (
                config.max_hold_bars is not None
                and index - entry_row >= config.max_hold_bars
            ):
                position = 0.0
                reason = "time_exit"
                exited = True

        if exited:
            entry_price = np.nan
            entry_row = None
            structure_stop_price = np.nan
            fixed_stop_price = np.nan
            best_close_move = 0.0
        else:
            if config.opposite_mode == "reverse":
                if long_values[index] and position != 1.0:
                    position = 1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = _structure_stop_price(
                        side=1,
                        entry_price=close,
                        source_price=valley_stops[index],
                    ) if config.structure_stop else np.nan
                    fixed_stop_price = close - config.fixed_stop_points if config.fixed_stop_points else np.nan
                    best_close_move = 0.0
                    reason = "long_signal"
                elif short_values[index] and position != -1.0:
                    position = -1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = _structure_stop_price(
                        side=-1,
                        entry_price=close,
                        source_price=peak_stops[index],
                    ) if config.structure_stop else np.nan
                    fixed_stop_price = close + config.fixed_stop_points if config.fixed_stop_points else np.nan
                    best_close_move = 0.0
                    reason = "short_signal"
            elif position == 0.0:
                if long_values[index]:
                    position = 1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = _structure_stop_price(
                        side=1,
                        entry_price=close,
                        source_price=valley_stops[index],
                    ) if config.structure_stop else np.nan
                    fixed_stop_price = close - config.fixed_stop_points if config.fixed_stop_points else np.nan
                    best_close_move = 0.0
                    reason = "long_signal"
                elif short_values[index]:
                    position = -1.0
                    entry_price = close
                    entry_row = index
                    structure_stop_price = _structure_stop_price(
                        side=-1,
                        entry_price=close,
                        source_price=peak_stops[index],
                    ) if config.structure_stop else np.nan
                    fixed_stop_price = close + config.fixed_stop_points if config.fixed_stop_points else np.nan
                    best_close_move = 0.0
                    reason = "short_signal"
            elif position > 0.0 and short_values[index]:
                position = 0.0
                entry_price = np.nan
                entry_row = None
                structure_stop_price = np.nan
                fixed_stop_price = np.nan
                best_close_move = 0.0
                reason = "opposite_flat_exit"
            elif position < 0.0 and long_values[index]:
                position = 0.0
                entry_price = np.nan
                entry_row = None
                structure_stop_price = np.nan
                fixed_stop_price = np.nan
                best_close_move = 0.0
                reason = "opposite_flat_exit"

        if position == 0.0:
            active_stop_price = np.nan
            current_bars_held = 0.0
        else:
            active_stop_price = _nearest_active_stop(
                side=int(position),
                structure_stop_price=structure_stop_price,
                fixed_stop_price=fixed_stop_price,
            )
            current_bars_held = float(index - entry_row) if entry_row is not None else 0.0

        trade_size = abs(position - start_position)
        strategy_return = (
            start_position * (event_price - previous_close) - trade_size * cost_per_trade
        )

        positions.append(position)
        event_reasons.append(reason)
        event_prices.append(float(event_price))
        active_stop_prices.append(active_stop_price)
        structure_stop_prices.append(
            float(structure_stop_price)
            if position != 0.0 and not np.isnan(structure_stop_price)
            else np.nan
        )
        fixed_stop_prices.append(
            float(fixed_stop_price)
            if position != 0.0 and not np.isnan(fixed_stop_price)
            else np.nan
        )
        trail_mfe_values.append(best_close_move if position != 0.0 else 0.0)
        trail_giveback_values.append(giveback if position != 0.0 else 0.0)
        strategy_returns.append(float(strategy_return))
        bars_held_values.append(current_bars_held)

    result = featured[["datetime", "open", "high", "low", "close", "volume"]].copy()
    result["strategy"] = strategy_name
    result["exit_config"] = config.name
    result["opposite_mode"] = config.opposite_mode
    result["structure_stop_enabled"] = config.structure_stop
    result["fixed_stop_points"] = config.fixed_stop_points
    result["trail_trigger_points"] = config.trail_trigger_points
    result["giveback_ratio"] = config.giveback_ratio
    result["max_hold_bars"] = config.max_hold_bars
    result["position"] = pd.Series(positions, index=featured.index, dtype="float64")
    result["event_reason"] = event_reasons
    result["event_price"] = event_prices
    result["active_stop_price"] = active_stop_prices
    result["structure_stop_price"] = structure_stop_prices
    result["fixed_stop_price"] = fixed_stop_prices
    result["trail_close_mfe_points"] = trail_mfe_values
    result["trail_close_giveback_points"] = trail_giveback_values
    result["bars_held"] = bars_held_values
    result["trade_size"] = result["position"].diff().abs().fillna(result["position"].abs())
    result["strategy_return_points"] = strategy_returns
    result["equity_points"] = result["strategy_return_points"].cumsum()
    return result


def _first_triggered_stop(
    *,
    side: int,
    low: float,
    high: float,
    structure_stop_price: float,
    fixed_stop_price: float,
) -> tuple[str | None, float]:
    candidates: list[tuple[str, float]] = []
    if not np.isnan(structure_stop_price):
        if side > 0 and low <= structure_stop_price:
            candidates.append(("structure_stop_exit", float(structure_stop_price)))
        elif side < 0 and high >= structure_stop_price:
            candidates.append(("structure_stop_exit", float(structure_stop_price)))
    if not np.isnan(fixed_stop_price):
        if side > 0 and low <= fixed_stop_price:
            candidates.append(("fixed_stop_exit", float(fixed_stop_price)))
        elif side < 0 and high >= fixed_stop_price:
            candidates.append(("fixed_stop_exit", float(fixed_stop_price)))
    if not candidates:
        return None, np.nan
    if side > 0:
        return min(candidates, key=lambda item: item[1])
    return max(candidates, key=lambda item: item[1])


def _structure_stop_price(side: int, entry_price: float, source_price: float) -> float:
    if np.isnan(source_price):
        return np.nan
    if side > 0:
        return float(source_price if source_price <= entry_price else entry_price - abs(entry_price - source_price))
    return float(source_price if source_price >= entry_price else entry_price + abs(entry_price - source_price))


def _nearest_active_stop(
    *,
    side: int,
    structure_stop_price: float,
    fixed_stop_price: float,
) -> float:
    stops = [value for value in [structure_stop_price, fixed_stop_price] if not np.isnan(value)]
    if not stops:
        return np.nan
    return float(max(stops) if side > 0 else min(stops))


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

    if current is not None:
        _close_trade(current, featured, strategy, len(strategy) - 1, current_return)
        rows.append(current)
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
        "strategy": strategy_row["strategy"],
        "exit_config": strategy_row["exit_config"],
        "opposite_mode": strategy_row["opposite_mode"],
        "structure_stop_enabled": bool(strategy_row["structure_stop_enabled"]),
        "fixed_stop_points": strategy_row["fixed_stop_points"],
        "trail_trigger_points": strategy_row["trail_trigger_points"],
        "giveback_ratio": strategy_row["giveback_ratio"],
        "max_hold_bars": strategy_row["max_hold_bars"],
        "entry_row": row_number,
        "entry_time": row["datetime"],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(row["close"]),
        "entry_reason": strategy_row["event_reason"],
        "entry_dc_state": row["dc_trend_state"],
        "entry_active_symbol": row.get("active_symbol", ""),
        "entry_active_stop_price": strategy_row["active_stop_price"],
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
            "holding_minutes": (exit_row - entry_row) * BASE_CANDIDATE.freq_minutes,
            "gross_return_points": gross_return,
            "net_return_points": float(net_return_points),
            "cost_drag_points": gross_return - float(net_return_points),
            "mfe_points": mfe,
            "mae_points": mae,
            "giveback_from_mfe_points": mfe - gross_return,
        }
    )


def _summary_rows(strategy_report: pd.DataFrame, trade_report: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        trades = (
            trade_report[trade_report["strategy"] == strategy_name]
            if "strategy" in trade_report
            else pd.DataFrame()
        )
        first_row = strategy.iloc[0]
        for period, period_strategy in _period_slices(strategy):
            period_trades = _period_trades(trades, period)
            rows.append(
                {
                    "strategy": strategy_name,
                    "exit_config": first_row["exit_config"],
                    "period": period,
                    "opposite_mode": first_row["opposite_mode"],
                    "structure_stop_enabled": bool(first_row["structure_stop_enabled"]),
                    "fixed_stop_points": first_row["fixed_stop_points"],
                    "trail_trigger_points": first_row["trail_trigger_points"],
                    "giveback_ratio": first_row["giveback_ratio"],
                    "max_hold_bars": first_row["max_hold_bars"],
                    "bsp_types": ",".join(MAIN_PARAMS.bsp_types),
                    "dc_threshold_points": MAIN_PARAMS.dc_threshold_points,
                    "obv_window": MAIN_PARAMS.obv_window,
                    "volume_strength": MAIN_PARAMS.volume_strength,
                    "volume_window": VOLUME_WINDOW,
                    **_strategy_metrics(period_strategy, period_trades),
                }
            )
    return pd.DataFrame(rows)


def _rank_exit_configs(summary: pd.DataFrame) -> pd.DataFrame:
    train = summary[summary["period"] == "train_2018_2022"].copy()
    test = summary[summary["period"] == "test_2023_2025YTD"].copy()
    keys = [
        "strategy",
        "exit_config",
        "opposite_mode",
        "structure_stop_enabled",
        "fixed_stop_points",
        "trail_trigger_points",
        "giveback_ratio",
        "max_hold_bars",
    ]
    merged = train.merge(test, on=keys, suffixes=("_train", "_test"))
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
        & (merged["total_return_points_test"] > 0)
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


def _strategy_metrics(strategy: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
    if strategy.empty:
        return _empty_metrics()
    returns = pd.to_numeric(strategy["strategy_return_points"], errors="coerce").fillna(0.0)
    equity = returns.cumsum()
    drawdown = equity.cummax() - equity
    trade_returns = (
        pd.to_numeric(trades["net_return_points"], errors="coerce").dropna()
        if not trades.empty and "net_return_points" in trades
        else pd.Series(dtype=float)
    )
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
        "mean_holding_bars": float(trades["holding_bars"].mean()) if not trades.empty else 0.0,
        "median_holding_bars": float(trades["holding_bars"].median()) if not trades.empty else 0.0,
        "mean_mfe_points": float(trades["mfe_points"].mean()) if not trades.empty else 0.0,
        "mean_mae_points": float(trades["mae_points"].mean()) if not trades.empty else 0.0,
        "mean_giveback_points": float(trades["giveback_from_mfe_points"].mean()) if not trades.empty else 0.0,
    }


def _period_slices(strategy: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    datetimes = _naive_datetimes(strategy["datetime"])
    train_mask = datetimes <= pd.Timestamp(TRAIN_END)
    test_mask = datetimes >= pd.Timestamp(TEST_START)
    return [
        ("train_2018_2022", strategy.loc[train_mask]),
        ("test_2023_2025YTD", strategy.loc[test_mask]),
        ("full_2018_2025YTD", strategy),
    ]


def _period_trades(trades: pd.DataFrame, period: str) -> pd.DataFrame:
    if trades.empty:
        return trades
    entry_times = _naive_datetimes(trades["entry_time"])
    if period == "train_2018_2022":
        return trades.loc[entry_times <= pd.Timestamp(TRAIN_END)]
    if period == "test_2023_2025YTD":
        return trades.loc[entry_times >= pd.Timestamp(TEST_START)]
    return trades


def _yearly_rows(strategy_report: pd.DataFrame, trade_report: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        strategy = strategy.copy()
        strategy["year"] = _naive_datetimes(strategy["datetime"]).dt.year
        trades = (
            trade_report[trade_report["strategy"] == strategy_name].copy()
            if "strategy" in trade_report
            else pd.DataFrame()
        )
        if not trades.empty:
            trades["year"] = _naive_datetimes(trades["entry_time"]).dt.year
        for year, year_strategy in strategy.groupby("year", sort=True):
            year_trades = trades[trades["year"] == year] if not trades.empty else pd.DataFrame()
            rows.append(
                {
                    "strategy": strategy_name,
                    "exit_config": year_strategy.iloc[0]["exit_config"],
                    "year": int(year),
                    **_strategy_metrics(year_strategy, year_trades),
                }
            )
    return pd.DataFrame(rows)


def _top_drawdown_rows(strategy_report: pd.DataFrame, trade_report: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        equity = strategy["equity_points"].reset_index(drop=True)
        drawdown = equity.cummax() - equity
        used_intervals: list[tuple[int, int]] = []
        trades = (
            trade_report[trade_report["strategy"] == strategy_name]
            if "strategy" in trade_report
            else pd.DataFrame()
        )
        rank = 0
        for trough in drawdown.sort_values(ascending=False).index:
            if drawdown.loc[trough] <= 0:
                break
            peak = int(equity.loc[:trough].idxmax())
            interval = (peak, int(trough))
            if any(not (interval[1] < used[0] or interval[0] > used[1]) for used in used_intervals):
                continue
            used_intervals.append(interval)
            overlapping = (
                trades[
                    (trades["entry_row"] <= interval[1])
                    & (trades["exit_row"] >= interval[0])
                ]
                if not trades.empty
                else pd.DataFrame()
            )
            rank += 1
            rows.append(
                {
                    "strategy": strategy_name,
                    "exit_config": strategy.iloc[0]["exit_config"],
                    "rank": rank,
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
            if rank >= 3:
                break
    return pd.DataFrame(rows)


def _signal_stats(
    featured: pd.DataFrame,
    chan_signals: pd.DataFrame,
    long_signal: pd.Series,
    short_signal: pd.Series,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strategy": MAIN_PARAMS.strategy_name,
                "bsp_types": ",".join(MAIN_PARAMS.bsp_types),
                "dc_threshold_points": MAIN_PARAMS.dc_threshold_points,
                "obv_window": MAIN_PARAMS.obv_window,
                "volume_strength": MAIN_PARAMS.volume_strength,
                "volume_window": VOLUME_WINDOW,
                "long_signals": int(long_signal.fillna(False).sum()),
                "short_signals": int(short_signal.fillna(False).sum()),
                "total_signals": int(long_signal.fillna(False).sum() + short_signal.fillna(False).sum()),
                "all_chan_bsp_buy": int(chan_signals["chan_bsp_buy"].fillna(False).sum()),
                "all_chan_bsp_sell": int(chan_signals["chan_bsp_sell"].fillna(False).sum()),
                "dc_bull_rows": int(featured["dc_is_bull"].sum()),
                "dc_bear_rows": int(featured["dc_is_bear"].sum()),
                "obv_above_ma_rows": int(featured["obv_above_ma"].sum()),
                "obv_below_ma_rows": int(featured["obv_below_ma"].sum()),
            }
        ]
    )


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
        "mean_holding_bars": 0.0,
        "median_holding_bars": 0.0,
        "mean_mfe_points": 0.0,
        "mean_mae_points": 0.0,
        "mean_giveback_points": 0.0,
    }


if __name__ == "__main__":
    main()
