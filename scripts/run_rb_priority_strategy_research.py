from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np
import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE
from data_foundation import aggregate_continuous_1m_to_Nm, load_continuous_1m
from chan_futures.feed import row_to_klu
from run_15m_sticky_exit_research import simulate_exit_strategy
from run_dc_structure_candidate_diagnostics import Candidate, _candidate_features


START = "2018-03-22 21:01:00"
END = "2025-04-03 15:00:00"
TRAIN_END = "2022-12-31 23:59:59"
TEST_START = "2023-01-01 00:00:00"

BASE_CANDIDATE = Candidate(
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
        description="Run the prioritized RB strategy research set on local continuous bars."
    )
    parser.add_argument(
        "--continuous-path",
        type=Path,
        default=Path("data/processed/RB_1m_continuous.parquet"),
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports/rb_priority_strategy_research"))
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--fee-points", type=float, default=1.0)
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--zs-breakout-buffer", type=float, default=5.0)
    parser.add_argument("--obv-window", type=int, default=40)
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
    featured = _append_obv_volume(
        _candidate_features(bars_15m, BASE_CANDIDATE),
        obv_window=args.obv_window,
        volume_window=args.volume_window,
    )

    chan_signals = _chan_signals(
        featured,
        kl_type=KL_TYPE.K_15M,
        zs_breakout_buffer=args.zs_breakout_buffer,
    )
    signal_sets = _build_signal_sets(featured, chan_signals)
    strategies = _simulate_strategies(
        featured,
        signal_sets,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )
    trades = pd.concat(
        [_extract_trades(featured, strategy) for strategy in strategies],
        ignore_index=True,
    )
    strategy_report = pd.concat(strategies, ignore_index=True)
    summary = _summary_rows(strategy_report, trades)
    signal_stats = _signal_stats(featured, signal_sets)
    yearly = _yearly_rows(strategy_report, trades)

    featured.to_csv(args.reports_dir / "rb_15m_featured.csv", index=False, encoding="utf-8-sig")
    chan_signals.to_csv(args.reports_dir / "rb_15m_chan_signals.csv", index=False, encoding="utf-8-sig")
    strategy_report.to_csv(
        args.reports_dir / "rb_priority_strategy_values.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades.to_csv(
        args.reports_dir / "rb_priority_strategy_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary.to_csv(
        args.reports_dir / "rb_priority_strategy_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    signal_stats.to_csv(
        args.reports_dir / "rb_priority_strategy_signal_stats.csv",
        index=False,
        encoding="utf-8-sig",
    )
    yearly.to_csv(
        args.reports_dir / "rb_priority_strategy_yearly.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"saved reports to: {args.reports_dir}")
    print(_compact_console_summary(summary))


def _build_signal_sets(featured: pd.DataFrame, chan_signals: pd.DataFrame) -> list[dict[str, object]]:
    chan_buy = chan_signals["chan_bsp_buy"]
    chan_sell = chan_signals["chan_bsp_sell"]
    trend_long = featured["trend_macd_golden_cross"] & featured["dc_is_bull"]
    trend_short = featured["trend_macd_death_cross"] & featured["dc_is_bear"]
    obv_volume_long = featured["obv_above_ma"] & featured["volume_above_ma"]
    obv_volume_short = featured["obv_below_ma"] & featured["volume_above_ma"]

    return [
        {
            "name": "01_chan_bsp_reverse",
            "long": chan_buy,
            "short": chan_sell,
            "structure_stop": False,
        },
        {
            "name": "02_chan_bsp_dc_filter",
            "long": chan_buy & featured["dc_is_bull"],
            "short": chan_sell & featured["dc_is_bear"],
            "structure_stop": False,
        },
        {
            "name": "03_trend_macd_dc_structure_stop",
            "long": trend_long,
            "short": trend_short,
            "structure_stop": True,
        },
        {
            "name": "04_chan_zs_breakout",
            "long": chan_signals["chan_zs_breakout_up"],
            "short": chan_signals["chan_zs_breakout_down"],
            "structure_stop": False,
        },
        {
            "name": "05_chan_bsp_dc_obv_volume",
            "long": chan_buy & featured["dc_is_bull"] & obv_volume_long,
            "short": chan_sell & featured["dc_is_bear"] & obv_volume_short,
            "structure_stop": False,
        },
        {
            "name": "06_trend_macd_dc_stop_obv_volume",
            "long": trend_long & obv_volume_long,
            "short": trend_short & obv_volume_short,
            "structure_stop": True,
        },
    ]


def _simulate_strategies(
    featured: pd.DataFrame,
    signal_sets: list[dict[str, object]],
    *,
    fee_points: float,
    slippage_points: float,
) -> list[pd.DataFrame]:
    strategies = []
    for signal_set in signal_sets:
        strategies.append(
            simulate_exit_strategy(
                featured,
                long_signal=signal_set["long"],
                short_signal=signal_set["short"],
                strategy_name=signal_set["name"],
                reverse_mode="reverse",
                trail_trigger_points=None,
                giveback_ratio=None,
                fee_points=fee_points,
                slippage_points=slippage_points,
                enable_structure_stop=bool(signal_set["structure_stop"]),
            )
        )
    return strategies


def _chan_signals(
    bars: pd.DataFrame,
    *,
    kl_type: KL_TYPE,
    zs_breakout_buffer: float,
) -> pd.DataFrame:
    chan = CChan(
        code="RB_MAIN",
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[kl_type],
        config=CChanConfig(
            {
                "trigger_step": True,
                "bi_strict": True,
                "divergence_rate": float("inf"),
                "bsp2_follow_1": False,
                "bsp3_follow_1": False,
                "min_zs_cnt": 0,
                "bs1_peak": False,
                "macd_algo": "peak",
                "bs_type": "1,1p,2,2s,3a,3b",
                "print_warning": False,
            }
        ),
        autype=AUTYPE.NONE,
    )

    rows: list[dict[str, object]] = []
    consumed_bsp_keys: set[tuple[int, int, str, bool]] = set()
    previous_close: float | None = None
    for row_number, row in bars.iterrows():
        chan.trigger_load({kl_type: [row_to_klu(row, kl_type=kl_type)]})
        close = float(row["close"])
        bsp_buy = bsp_sell = False
        bsp_type = None
        bsp_bi_idx = None
        bsp_klu_idx = None
        latest_bsp = chan.get_latest_bsp(idx=0, number=1)
        if latest_bsp:
            bsp = latest_bsp[0]
            key = (bsp.bi.idx, bsp.klu.idx, bsp.type2str(), bsp.is_buy)
            if key not in consumed_bsp_keys and _bsp_is_confirmed(chan[0], bsp):
                consumed_bsp_keys.add(key)
                bsp_buy = bool(bsp.is_buy)
                bsp_sell = not bsp.is_buy
                bsp_type = bsp.type2str()
                bsp_bi_idx = bsp.bi.idx
                bsp_klu_idx = bsp.klu.idx

        zs_up = zs_down = False
        zs_low = zs_high = np.nan
        if len(chan[0].zs_list) and previous_close is not None:
            zs = chan[0].zs_list[-1]
            zs_low = float(zs.low)
            zs_high = float(zs.high)
            zs_up = previous_close <= zs_high and close > zs_high + zs_breakout_buffer
            zs_down = previous_close >= zs_low and close < zs_low - zs_breakout_buffer
        previous_close = close

        rows.append(
            {
                "row_number": row_number,
                "datetime": row["datetime"],
                "chan_bsp_buy": bsp_buy,
                "chan_bsp_sell": bsp_sell,
                "chan_bsp_type": bsp_type,
                "chan_bsp_bi_idx": bsp_bi_idx,
                "chan_bsp_klu_idx": bsp_klu_idx,
                "chan_zs_breakout_up": zs_up,
                "chan_zs_breakout_down": zs_down,
                "chan_zs_low": zs_low,
                "chan_zs_high": zs_high,
            }
        )
    return pd.DataFrame(rows)


def _append_obv_volume(
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
    result["volume_above_ma"] = volume > result["volume_ma"]
    return result


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
        "entry_row": row_number,
        "entry_time": row["datetime"],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(row["close"]),
        "entry_reason": strategy_row["event_reason"],
        "entry_dc_state": row["dc_trend_state"],
        "entry_active_symbol": row.get("active_symbol", ""),
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
            "net_return_points": float(net_return_points),
            "mfe_points": mfe,
            "mae_points": mae,
        }
    )


def _summary_rows(strategy_report: pd.DataFrame, trades_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        trades = trades_report[trades_report["strategy"] == strategy_name]
        for period, period_strategy in _period_slices(strategy):
            period_trades = _period_trades(trades, period)
            rows.append(
                {
                    "strategy": strategy_name,
                    "period": period,
                    **_strategy_metrics(period_strategy, period_trades),
                }
            )
    return pd.DataFrame(rows)


def _yearly_rows(strategy_report: pd.DataFrame, trades_report: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy_name, strategy in strategy_report.groupby("strategy", sort=False):
        strategy = strategy.copy()
        strategy["year"] = _naive_datetimes(strategy["datetime"]).dt.year
        trades = trades_report[trades_report["strategy"] == strategy_name].copy()
        if not trades.empty:
            trades["year"] = _naive_datetimes(trades["entry_time"]).dt.year
        for year, year_strategy in strategy.groupby("year", sort=True):
            year_trades = trades[trades["year"] == year] if not trades.empty else pd.DataFrame()
            rows.append(
                {
                    "strategy": strategy_name,
                    "year": int(year),
                    **_strategy_metrics(year_strategy, year_trades),
                }
            )
    return pd.DataFrame(rows)


def _signal_stats(featured: pd.DataFrame, signal_sets: list[dict[str, object]]) -> pd.DataFrame:
    rows = []
    for signal_set in signal_sets:
        long_signal = signal_set["long"].fillna(False)
        short_signal = signal_set["short"].fillna(False)
        rows.append(
            {
                "strategy": signal_set["name"],
                "long_signals": int(long_signal.sum()),
                "short_signals": int(short_signal.sum()),
                "total_signals": int(long_signal.sum() + short_signal.sum()),
                "dc_bull_rows": int(featured["dc_is_bull"].sum()),
                "dc_bear_rows": int(featured["dc_is_bear"].sum()),
                "obv_volume_long_rows": int((featured["obv_above_ma"] & featured["volume_above_ma"]).sum()),
                "obv_volume_short_rows": int((featured["obv_below_ma"] & featured["volume_above_ma"]).sum()),
            }
        )
    return pd.DataFrame(rows)


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


def _restrict_range(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    result = frame.copy()
    datetimes = _naive_datetimes(result["datetime"])
    result["datetime"] = datetimes
    return result[(datetimes >= pd.Timestamp(start)) & (datetimes <= pd.Timestamp(end))].reset_index(drop=True)


def _naive_datetimes(values: pd.Series) -> pd.Series:
    result = pd.to_datetime(values)
    if getattr(result.dt, "tz", None) is not None:
        result = result.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return result


def _bsp_is_confirmed(kl_list, bsp) -> bool:
    return len(kl_list) >= 2 and bsp.klu.klc.idx == kl_list[-2].idx


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


def _compact_console_summary(summary: pd.DataFrame) -> str:
    selected = summary[summary["period"].isin(["train_2018_2022", "test_2023_2025YTD"])].copy()
    cols = [
        "strategy",
        "period",
        "trade_count",
        "total_return_points",
        "max_drawdown_points",
        "return_drawdown_ratio",
    ]
    return selected[cols].to_string(index=False)


if __name__ == "__main__":
    main()
