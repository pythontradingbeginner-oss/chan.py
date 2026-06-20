from __future__ import annotations

from typing import Any, Literal, Tuple

import pandas as pd


PRICE_COLUMNS = ["open", "high", "low", "close"]


def build_continuous_contract(
    cleaned_df: pd.DataFrame,
    *,
    method: Literal["backward_adjusted", "no_adjust"] = "backward_adjusted",
    volume_threshold: int = 100_000,
    cooldown_days: int = 5,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Build a one-minute RB main-contract continuous series.

    Args:
        cleaned_df: Cleaned multi-contract one-minute bars.
        method: Price adjustment method. Use backward_adjusted for additive
            close-difference back adjustment, or no_adjust for raw splicing.
        volume_threshold: Minimum daily main-contract volume.
        cooldown_days: Minimum holding trading days before another roll.

    Returns:
        The continuous one-minute bars and the switch log.
    """
    if method not in {"backward_adjusted", "no_adjust"}:
        raise ValueError(f"unsupported method: {method}")
    _require_columns(cleaned_df)

    source = _prepare_source(cleaned_df)
    selected_days, _, rollovers = _select_main_contract_days(
        source,
        min_daily_main_volume=volume_threshold,
        rollover_cooldown_trading_days=cooldown_days,
    )
    if selected_days.empty:
        return _empty_continuous(), _empty_switch_log()

    selected = source.merge(
        selected_days[["trading_day", "symbol"]],
        on=["trading_day", "symbol"],
        how="inner",
    ).sort_values("datetime")
    selected = selected.rename(columns={"symbol": "active_symbol"}).reset_index(drop=True)

    adjusted, switch_log = _apply_adjustments_1m(
        selected,
        source,
        rollovers,
        adjust=(method == "backward_adjusted"),
    )
    return adjusted, switch_log


def _prepare_source(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["symbol"] = result["symbol"].astype(str).str.upper()
    result["datetime"] = pd.to_datetime(result["datetime"])
    for column in PRICE_COLUMNS + ["volume", "open_interest"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if "flags" not in result.columns:
        result["flags"] = 0
    if "trading_day" not in result.columns:
        result["trading_day"] = _assign_trading_day(result["datetime"])
    else:
        result["trading_day"] = pd.to_datetime(result["trading_day"]).dt.normalize()
    return result.sort_values(["datetime", "symbol"]).reset_index(drop=True)


def _select_main_contract_days(
    bars: pd.DataFrame,
    min_daily_main_volume: float = 100_000,
    rollover_cooldown_trading_days: int = 5,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    if min_daily_main_volume <= 0:
        raise ValueError("min_daily_main_volume must be positive")
    if rollover_cooldown_trading_days <= 0:
        raise ValueError("rollover_cooldown_trading_days must be positive")

    daily = (
        bars.groupby(["trading_day", "symbol"], as_index=False)["volume"]
        .sum()
        .sort_values(["trading_day", "volume", "symbol"])
    )
    leader_indexes = daily.groupby("trading_day")["volume"].idxmax()
    leaders = daily.loc[leader_indexes].sort_values("trading_day")
    volume_lookup = {
        (row.trading_day, row.symbol): float(row.volume)
        for row in daily.itertuples()
    }

    current_contract: str | None = None
    cooldown_days_held = 0
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    rollovers: list[dict[str, Any]] = []

    for leader in leaders.itertuples():
        trading_day = pd.Timestamp(leader.trading_day)
        candidate_contract = str(leader.symbol)
        candidate_volume = float(leader.volume)
        candidate_eligible = candidate_volume >= min_daily_main_volume

        if current_contract is None:
            if not candidate_eligible:
                excluded.append({"trading_day": trading_day.date()})
                continue
            current_contract = candidate_contract
            cooldown_days_held = 1
        elif cooldown_days_held < rollover_cooldown_trading_days:
            cooldown_days_held += 1
        elif not candidate_eligible:
            cooldown_days_held += 1
            excluded.append({"trading_day": trading_day.date()})
            continue
        elif candidate_contract != current_contract:
            previous_contract = current_contract
            current_contract = candidate_contract
            cooldown_days_held = 1
            rollovers.append(
                {
                    "trading_day": trading_day,
                    "from_contract_id": previous_contract,
                    "to_contract_id": current_contract,
                }
            )
        else:
            cooldown_days_held += 1

        current_volume = volume_lookup.get((trading_day, current_contract), 0.0)
        if current_volume < min_daily_main_volume:
            excluded.append({"trading_day": trading_day.date()})
            continue
        selected.append({"trading_day": trading_day, "symbol": current_contract})

    return pd.DataFrame(selected), excluded, rollovers


def _apply_adjustments_1m(
    selected: pd.DataFrame,
    all_bars: pd.DataFrame,
    rollovers: list[dict[str, Any]],
    *,
    adjust: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    result = selected.copy()
    for column in PRICE_COLUMNS:
        result[f"raw_{column}"] = result[column]
    result["adjustment_points"] = 0.0

    switch_rows: list[dict[str, Any]] = []
    cumulative_adjustment = 0.0
    for rollover in rollovers:
        roll_day = pd.Timestamp(rollover["trading_day"])
        old_symbol = str(rollover["from_contract_id"])
        new_symbol = str(rollover["to_contract_id"])
        old_close, new_close, basis_day = _latest_common_pre_roll_closes(
            all_bars,
            roll_day,
            old_symbol,
            new_symbol,
        )
        price_diff = new_close - old_close
        if adjust:
            result.loc[result["trading_day"] < roll_day, "adjustment_points"] += price_diff
            cumulative_adjustment += price_diff
        switch_rows.append(
            {
                "switch_date": roll_day.date(),
                "old_symbol": old_symbol,
                "new_symbol": new_symbol,
                "old_close": old_close,
                "new_close": new_close,
                "price_diff": price_diff,
                "cumulative_adjustment": cumulative_adjustment,
                "basis_date": basis_day.date(),
            }
        )

    if adjust:
        for column in PRICE_COLUMNS:
            result[column] = result[f"raw_{column}"] + result["adjustment_points"]
    else:
        result["adjustment_points"] = 0.0

    columns = [
        "datetime",
        "trading_day",
        "open",
        "high",
        "low",
        "close",
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "volume",
        "open_interest",
        "active_symbol",
        "flags",
        "adjustment_points",
    ]
    optional = [column for column in ["turnover", "exchange"] if column in result.columns]
    result = result[columns + optional].reset_index(drop=True)
    return result, pd.DataFrame(switch_rows, columns=list(_empty_switch_log().columns))


def _latest_common_pre_roll_closes(
    all_bars: pd.DataFrame,
    roll_day: pd.Timestamp,
    old_symbol: str,
    new_symbol: str,
) -> tuple[float, float, pd.Timestamp]:
    prior = all_bars[
        (all_bars["trading_day"] < roll_day)
        & all_bars["symbol"].isin([old_symbol, new_symbol])
    ]
    common_days = prior.groupby("trading_day")["symbol"].nunique()
    common_days = common_days[common_days == 2]
    if common_days.empty:
        raise ValueError(f"no common pre-roll close for {old_symbol}->{new_symbol}")

    basis_day = pd.Timestamp(common_days.index.max())
    closes = (
        prior[prior["trading_day"] == basis_day]
        .sort_values("datetime")
        .groupby("symbol")
        .tail(1)
        .set_index("symbol")["close"]
    )
    return float(closes[old_symbol]), float(closes[new_symbol]), basis_day


def _assign_trading_day(datetimes: pd.Series) -> pd.Series:
    naive = pd.to_datetime(datetimes)
    if getattr(naive.dt, "tz", None) is not None:
        naive = naive.dt.tz_localize(None)
    night = naive.dt.hour >= 21
    base = naive.dt.normalize()
    return base.where(~night, base + pd.Timedelta(days=1))


def _require_columns(df: pd.DataFrame) -> None:
    required = {
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"missing required columns: {missing}")


def _empty_continuous() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "datetime",
            "trading_day",
            "open",
            "high",
            "low",
            "close",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "volume",
            "open_interest",
            "active_symbol",
            "flags",
            "adjustment_points",
        ]
    )


def _empty_switch_log() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "switch_date",
            "old_symbol",
            "new_symbol",
            "old_close",
            "new_close",
            "price_diff",
            "cumulative_adjustment",
            "basis_date",
        ]
    )
