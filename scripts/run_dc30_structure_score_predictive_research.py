from __future__ import annotations

import argparse
import sys
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
SCORE_BINS = [-np.inf, -1.5, -0.75, -0.25, 0.25, 0.75, 1.5, np.inf]
SCORE_LABELS = [
    "strong_bear",
    "bear",
    "slight_bear",
    "neutral",
    "slight_bull",
    "bull",
    "strong_bull",
]
ALIGNMENT_LABELS = [
    "strong_against",
    "against",
    "slight_against",
    "neutral",
    "slight_with",
    "with",
    "strong_with",
]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate whether 30m DC structure score predicts 5m trend MACD trades."
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

    all_trades: list[pd.DataFrame] = []
    all_bins: list[dict[str, object]] = []
    all_summary: list[dict[str, object]] = []

    for threshold_points in [20.0, 30.0, 40.0]:
        dc30 = add_dc_pivots(bars_30m, threshold_points=threshold_points)
        dc30 = add_dc_structure_score(dc30, threshold_points=threshold_points)
        featured = map_higher_timeframe_columns(
            featured_5m,
            dc30,
            higher_freq_minutes=30,
            columns=[
                "dc_structure_score",
                "dc_peak_delta_points",
                "dc_valley_delta_points",
            ],
            prefix="dc30",
        )
        trades = extract_trend_macd_signal_trades(
            featured,
            score_col="dc30_structure_score",
            fee_points=args.fee_points,
            slippage_points=args.slippage_points,
        )
        trades["dc_threshold_points"] = threshold_points
        all_trades.append(trades)

        all_bins.extend(_bin_rows(trades))
        all_summary.extend(_summary_rows(trades))

    trade_report = pd.concat(all_trades, ignore_index=True)
    trade_report.to_csv(
        args.reports_dir / "dc30_structure_score_predictive_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_bins).to_csv(
        args.reports_dir / "dc30_structure_score_predictive_bins.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(all_summary).to_csv(
        args.reports_dir / "dc30_structure_score_predictive_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print(f"saved reports to: {args.reports_dir}")


def add_dc_structure_score(
    dc_frame: pd.DataFrame,
    *,
    threshold_points: float,
) -> pd.DataFrame:
    """Append a simple confirmed-pivot structure score to DC pivot bars.

    score = (latest_peak_delta + latest_valley_delta) / (2 * threshold_points)

    A positive score means both recent same-kind pivots lean upward in aggregate;
    a negative score means they lean downward in aggregate. Values are only
    available after two confirmed peaks and two confirmed valleys, and are
    forward-filled from confirmation bars only.
    """
    if threshold_points <= 0:
        raise ValueError("threshold_points must be positive")
    required = ["dc_pivot_kind", "dc_pivot_price"]
    missing = [column for column in required if column not in dc_frame.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")

    result = dc_frame.copy()
    result["dc_peak_delta_points"] = np.nan
    result["dc_valley_delta_points"] = np.nan
    result["dc_structure_score"] = np.nan

    peaks: list[float] = []
    valleys: list[float] = []
    current_peak_delta = np.nan
    current_valley_delta = np.nan
    current_score = np.nan

    for row_number, row in enumerate(result.itertuples(index=False)):
        kind = getattr(row, "dc_pivot_kind")
        price = getattr(row, "dc_pivot_price")
        if pd.notna(kind) and pd.notna(price):
            if kind == "peak":
                peaks.append(float(price))
                if len(peaks) >= 2:
                    current_peak_delta = peaks[-1] - peaks[-2]
            elif kind == "valley":
                valleys.append(float(price))
                if len(valleys) >= 2:
                    current_valley_delta = valleys[-1] - valleys[-2]
            else:
                raise ValueError(f"unsupported pivot kind: {kind}")

            if not pd.isna(current_peak_delta) and not pd.isna(current_valley_delta):
                current_score = (current_peak_delta + current_valley_delta) / (
                    2 * float(threshold_points)
                )

        result.iat[row_number, result.columns.get_loc("dc_peak_delta_points")] = current_peak_delta
        result.iat[row_number, result.columns.get_loc("dc_valley_delta_points")] = current_valley_delta
        result.iat[row_number, result.columns.get_loc("dc_structure_score")] = current_score

    return result


def map_higher_timeframe_columns(
    lower_frame: pd.DataFrame,
    higher_frame: pd.DataFrame,
    *,
    higher_freq_minutes: int,
    columns: list[str],
    datetime_col: str = "datetime",
    prefix: str = "dc30",
) -> pd.DataFrame:
    missing_lower = [column for column in [datetime_col] if column not in lower_frame.columns]
    missing_higher = [
        column for column in [datetime_col, *columns] if column not in higher_frame.columns
    ]
    if missing_lower:
        raise ValueError(f"lower frame missing required column(s): {', '.join(missing_lower)}")
    if missing_higher:
        raise ValueError(f"higher frame missing required column(s): {', '.join(missing_higher)}")

    result = lower_frame.copy()
    result["__row_order"] = np.arange(len(result))
    result["__lower_time"] = _naive_datetimes(result[datetime_col])

    available_col = f"{prefix}_available_at"
    output_columns = {column: f"{prefix}_{_strip_dc_prefix(column)}" for column in columns}

    if higher_frame.empty:
        for output_col in output_columns.values():
            result[output_col] = np.nan
        result[available_col] = pd.NaT
        return result.drop(columns=["__row_order", "__lower_time"])

    higher = higher_frame[[datetime_col, *columns]].copy()
    higher["__available_at"] = _naive_datetimes(higher[datetime_col]) + pd.Timedelta(
        minutes=higher_freq_minutes
    )
    higher = higher.rename(columns={**output_columns, "__available_at": available_col})
    higher = higher[[available_col, *output_columns.values()]]

    merged = pd.merge_asof(
        result.sort_values("__lower_time"),
        higher.sort_values(available_col),
        left_on="__lower_time",
        right_on=available_col,
        direction="backward",
    )
    return (
        merged.sort_values("__row_order")
        .drop(columns=["__row_order", "__lower_time"])
        .reset_index(drop=True)
    )


def extract_trend_macd_signal_trades(
    featured: pd.DataFrame,
    *,
    score_col: str,
    fee_points: float,
    slippage_points: float,
) -> pd.DataFrame:
    required = [
        "datetime",
        "close",
        "trend_macd_golden_cross",
        "trend_macd_death_cross",
        score_col,
    ]
    missing = [column for column in required if column not in featured.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")

    signal = pd.Series(0, index=featured.index, dtype="int64")
    signal.loc[featured["trend_macd_golden_cross"].fillna(False)] = 1
    signal.loc[featured["trend_macd_death_cross"].fillna(False)] = -1

    position = signal.replace(0, np.nan).ffill().fillna(0.0).astype("float64")
    cost = float(fee_points + slippage_points)
    trade_size = position.diff().abs().fillna(position.abs())
    bar_return = pd.to_numeric(featured["close"], errors="coerce").diff().fillna(0.0)
    strategy_return = position.shift(1).fillna(0.0) * bar_return - trade_size * cost

    rows = []
    current: dict[str, object] | None = None
    current_net_return = 0.0
    previous_sign = 0
    datetimes = featured["datetime"].to_numpy()
    closes = pd.to_numeric(featured["close"], errors="coerce").to_numpy(dtype=float)
    scores = pd.to_numeric(featured[score_col], errors="coerce").to_numpy(dtype=float)

    for row_number, (pos, row_return) in enumerate(zip(position.to_numpy(), strategy_return)):
        sign = int(np.sign(pos))
        if current is None and sign != 0:
            current = _new_trade(row_number, sign, datetimes, closes, scores)
            current_net_return = 0.0

        if current is not None:
            current_net_return += float(row_return)

        if current is not None and previous_sign != 0 and sign != previous_sign:
            current.update(
                {
                    "exit_row": row_number,
                    "exit_time": datetimes[row_number],
                    "exit_close": float(closes[row_number]),
                    "gross_return_points": current["side"]
                    * (float(closes[row_number]) - current["entry_close"]),
                    "net_return_points": current_net_return,
                    "holding_bars": row_number - int(current["entry_row"]),
                }
            )
            current["cost_points"] = current["gross_return_points"] - current_net_return
            rows.append(current)
            current = _new_trade(row_number, sign, datetimes, closes, scores) if sign != 0 else None
            current_net_return = 0.0

        previous_sign = sign

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades

    trades["score_bin"] = pd.cut(
        trades["dc30_structure_score"],
        bins=SCORE_BINS,
        labels=SCORE_LABELS,
    )
    trades["alignment_bin"] = pd.cut(
        trades["directional_alignment_score"],
        bins=SCORE_BINS,
        labels=ALIGNMENT_LABELS,
    )
    trades["period_2018_2022"] = _between(
        trades["entry_time"],
        "2018-01-01",
        "2022-12-31 23:59:59",
    )
    trades["period_2023_2025YTD"] = _naive_datetimes(trades["entry_time"]) >= pd.Timestamp(
        "2023-01-01"
    )
    return trades


def _new_trade(
    row_number: int,
    side: int,
    datetimes: np.ndarray,
    closes: np.ndarray,
    scores: np.ndarray,
) -> dict[str, object]:
    score = float(scores[row_number]) if not pd.isna(scores[row_number]) else np.nan
    return {
        "entry_row": row_number,
        "entry_time": datetimes[row_number],
        "side": side,
        "side_name": "long" if side > 0 else "short",
        "entry_close": float(closes[row_number]),
        "dc30_structure_score": score,
        "directional_alignment_score": side * score if not pd.isna(score) else np.nan,
    }


def _bin_rows(trades: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    for period, period_trades in _period_slices(trades).items():
        rows.extend(
            _group_rows(
                period_trades,
                period=period,
                grouping="score_by_side",
                group_cols=["side_name", "score_bin"],
            )
        )
        rows.extend(
            _group_rows(
                period_trades,
                period=period,
                grouping="alignment",
                group_cols=["alignment_bin"],
            )
        )
    return rows


def _summary_rows(trades: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    for period, period_trades in _period_slices(trades).items():
        valid = period_trades.dropna(subset=["directional_alignment_score", "net_return_points"])
        if valid.empty:
            rows.append(_empty_summary_row(trades, period_trades, period))
            continue

        alignment_group = (
            valid.groupby("alignment_bin", observed=False)["net_return_points"]
            .mean()
            .reindex(ALIGNMENT_LABELS)
        )
        bin_rank = pd.Series(range(len(alignment_group)), index=alignment_group.index)
        monotonic_corr = _rank_corr(alignment_group, bin_rank)
        rows.append(
            {
                "dc_threshold_points": float(trades["dc_threshold_points"].iloc[0]),
                "period": period,
                "trade_count": float(len(valid)),
                "missing_score_count": float(len(period_trades) - len(valid)),
                "mean_return_points": float(valid["net_return_points"].mean()),
                "win_rate": float((valid["net_return_points"] > 0).mean()),
                "alignment_pearson_corr": _safe_corr(
                    valid["directional_alignment_score"],
                    valid["net_return_points"],
                    method="pearson",
                ),
                "alignment_spearman_corr": _safe_corr(
                    valid["directional_alignment_score"],
                    valid["net_return_points"],
                    method="spearman",
                ),
                "alignment_bin_mean_spearman_corr": (
                    float(monotonic_corr) if not pd.isna(monotonic_corr) else 0.0
                ),
                "strong_against_mean_return": _group_mean(valid, "alignment_bin", "strong_against"),
                "strong_with_mean_return": _group_mean(valid, "alignment_bin", "strong_with"),
                "with_minus_against_mean_return": (
                    _group_mean(valid, "alignment_bin", "strong_with")
                    - _group_mean(valid, "alignment_bin", "strong_against")
                ),
            }
        )
    return rows


def _group_rows(
    trades: pd.DataFrame,
    *,
    period: str,
    grouping: str,
    group_cols: list[str],
) -> list[dict[str, object]]:
    rows = []
    grouped = trades.groupby(group_cols, observed=False, dropna=False)
    for group_key, group in grouped:
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        row = {
            "dc_threshold_points": float(trades["dc_threshold_points"].iloc[0]),
            "period": period,
            "grouping": grouping,
            "trade_count": float(len(group)),
            "mean_return_points": float(group["net_return_points"].mean()) if len(group) else 0.0,
            "median_return_points": float(group["net_return_points"].median()) if len(group) else 0.0,
            "win_rate": float((group["net_return_points"] > 0).mean()) if len(group) else 0.0,
            "profit_factor": _profit_factor(group["net_return_points"]),
            "mean_score": float(group["dc30_structure_score"].mean()) if len(group) else 0.0,
            "mean_alignment_score": (
                float(group["directional_alignment_score"].mean()) if len(group) else 0.0
            ),
        }
        for col, value in zip(group_cols, group_key):
            row[col] = value
        rows.append(row)
    return rows


def _period_slices(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "2018-2022": trades[trades["period_2018_2022"]].copy(),
        "2023-2025YTD": trades[trades["period_2023_2025YTD"]].copy(),
        "full_2018-2025YTD": trades.copy(),
    }


def _profit_factor(returns: pd.Series) -> float:
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if wins.empty or losses.empty:
        return 0.0
    return float(wins.sum() / abs(losses.sum()))


def _safe_corr(left: pd.Series, right: pd.Series, *, method: str) -> float:
    if len(left) < 2 or left.nunique(dropna=True) < 2 or right.nunique(dropna=True) < 2:
        return 0.0
    if method == "spearman":
        value = _rank_corr(left, right)
    else:
        value = left.corr(right, method=method)
    return float(value) if not pd.isna(value) else 0.0


def _rank_corr(left: pd.Series, right: pd.Series) -> float:
    values = pd.concat(
        [
            pd.to_numeric(left, errors="coerce").rename("left"),
            pd.to_numeric(right, errors="coerce").rename("right"),
        ],
        axis=1,
    ).dropna()
    if len(values) < 2:
        return 0.0
    left_rank = values["left"].rank(method="average")
    right_rank = values["right"].rank(method="average")
    if left_rank.nunique() < 2 or right_rank.nunique() < 2:
        return 0.0
    value = left_rank.corr(right_rank)
    return float(value) if not pd.isna(value) else 0.0


def _group_mean(frame: pd.DataFrame, column: str, value: str) -> float:
    group = frame[frame[column].astype(str) == value]
    if group.empty:
        return 0.0
    return float(group["net_return_points"].mean())


def _empty_summary_row(
    all_trades: pd.DataFrame,
    period_trades: pd.DataFrame,
    period: str,
) -> dict[str, object]:
    return {
        "dc_threshold_points": float(all_trades["dc_threshold_points"].iloc[0]),
        "period": period,
        "trade_count": 0.0,
        "missing_score_count": float(len(period_trades)),
        "mean_return_points": 0.0,
        "win_rate": 0.0,
        "alignment_pearson_corr": 0.0,
        "alignment_spearman_corr": 0.0,
        "alignment_bin_mean_spearman_corr": 0.0,
        "strong_against_mean_return": 0.0,
        "strong_with_mean_return": 0.0,
        "with_minus_against_mean_return": 0.0,
    }


def _between(values: pd.Series, start: str, end: str) -> pd.Series:
    datetimes = _naive_datetimes(values)
    return (datetimes >= pd.Timestamp(start)) & (datetimes <= pd.Timestamp(end))


def _restrict_research_window(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["datetime"] = _naive_datetimes(result["datetime"])
    return result[result["datetime"] >= pd.Timestamp(RESEARCH_START)].reset_index(drop=True)


def _strip_dc_prefix(column: str) -> str:
    return column[3:] if column.startswith("dc_") else column


def _naive_datetimes(values: pd.Series) -> pd.Series:
    result = pd.to_datetime(values)
    if getattr(result.dt, "tz", None) is not None:
        result = result.dt.tz_localize(None)
    return result


if __name__ == "__main__":
    main()
