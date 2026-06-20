from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd


EXTREME_MOVE = 1
LIMIT_UP = 2
LIMIT_DOWN = 4
MISSING_NEIGHBOR = 8
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRICE_COLUMNS = ["open", "high", "low", "close"]


@dataclass
class CleaningLog:
    """Audit counters produced by the RB one-minute cleaning pipeline."""

    raw_rows: int
    deduplicated: int
    invalid_price_removed: int
    invalid_ohlc_removed: int
    extreme_moves_flagged: int
    limit_up_flagged: int
    limit_down_flagged: int
    missing_bars_per_day: Dict[date, int]
    final_rows: int

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return {
            "raw_rows": self.raw_rows,
            "deduplicated": self.deduplicated,
            "invalid_price_removed": self.invalid_price_removed,
            "invalid_ohlc_removed": self.invalid_ohlc_removed,
            "extreme_moves_flagged": self.extreme_moves_flagged,
            "limit_up_flagged": self.limit_up_flagged,
            "limit_down_flagged": self.limit_down_flagged,
            "missing_bars_per_day": {
                key.isoformat(): value
                for key, value in self.missing_bars_per_day.items()
            },
            "final_rows": self.final_rows,
        }

    def to_markdown(self) -> str:
        """Render a compact markdown summary."""
        rows = [
            ("Raw rows", self.raw_rows),
            ("Duplicates removed", self.deduplicated),
            ("Invalid price rows removed", self.invalid_price_removed),
            ("Invalid OHLC rows removed", self.invalid_ohlc_removed),
            ("Extreme moves flagged", self.extreme_moves_flagged),
            ("Limit-up rows flagged", self.limit_up_flagged),
            ("Limit-down rows flagged", self.limit_down_flagged),
            ("Final rows", self.final_rows),
        ]
        lines = ["| Metric | Value |", "| --- | ---: |"]
        lines.extend(f"| {name} | {value} |" for name, value in rows)
        return "\n".join(lines)


def clean_rb_1m_bars(
    raw_df: pd.DataFrame,
    *,
    extreme_return_threshold: float = 0.05,
    drop_invalid_ohlc: bool = True,
) -> Tuple[pd.DataFrame, CleaningLog]:
    """Clean multi-contract RB one-minute bars and attach audit flags.

    Args:
        raw_df: Raw one-minute bars with standard columns:
            symbol, datetime, open, high, low, close, volume, open_interest.
        extreme_return_threshold: Absolute close-to-close return threshold used
            to flag suspicious jumps without removing them.
        drop_invalid_ohlc: Whether to remove rows with inconsistent OHLC values.

    Returns:
        A tuple of cleaned bars and a CleaningLog audit object.
    """
    _require_columns(raw_df)
    if extreme_return_threshold <= 0:
        raise ValueError("extreme_return_threshold must be positive")

    raw_rows = len(raw_df)
    df = _standardize_frame(raw_df)

    before = len(df)
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")
    deduplicated = before - len(df)

    invalid_price_mask = (df[PRICE_COLUMNS] <= 0).any(axis=1)
    invalid_price_removed = int(invalid_price_mask.sum())
    df = df.loc[~invalid_price_mask].copy()

    invalid_ohlc_mask = _invalid_ohlc_mask(df)
    invalid_ohlc_removed = int(invalid_ohlc_mask.sum()) if drop_invalid_ohlc else 0
    if drop_invalid_ohlc:
        df = df.loc[~invalid_ohlc_mask].copy()

    df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)
    df["flags"] = 0
    df["trading_day"] = _assign_trading_day(df["datetime"])

    returns = df.groupby("symbol")["close"].pct_change()
    extreme_mask = returns.abs() > extreme_return_threshold
    df.loc[extreme_mask.fillna(False), "flags"] |= EXTREME_MOVE

    limit_up_mask, limit_down_mask = _limit_masks(df)
    df.loc[limit_up_mask, "flags"] |= LIMIT_UP
    df.loc[limit_down_mask, "flags"] |= LIMIT_DOWN

    missing = detect_missing_minutes(df)
    _flag_missing_neighbors(df, missing)

    log = CleaningLog(
        raw_rows=raw_rows,
        deduplicated=deduplicated,
        invalid_price_removed=invalid_price_removed,
        invalid_ohlc_removed=invalid_ohlc_removed,
        extreme_moves_flagged=int(extreme_mask.fillna(False).sum()),
        limit_up_flagged=int(limit_up_mask.sum()),
        limit_down_flagged=int(limit_down_mask.sum()),
        missing_bars_per_day={
            day: len(minutes)
            for day, minutes in sorted(missing.items())
            if minutes
        },
        final_rows=len(df),
    )
    return df.reset_index(drop=True), log


def get_rb_standard_minutes(trading_date: date) -> List[time]:
    """Return RB expected end-labelled one-minute bar times for a trading day.

    The list uses vn.py imported one-minute bar end labels. Weekends return an
    empty list; exchange holidays are not modeled locally.
    """
    if trading_date.weekday() >= 5:
        return []

    sessions = [
        (time(21, 1), time(23, 0)),
        (time(9, 1), time(10, 15)),
        (time(10, 31), time(11, 30)),
        (time(13, 31), time(15, 0)),
    ]
    minutes: list[time] = []
    for start, end in sessions:
        current = datetime.combine(trading_date, start)
        stop = datetime.combine(trading_date, end)
        while current <= stop:
            minutes.append(current.time())
            current += timedelta(minutes=1)
    return minutes


def detect_missing_minutes(df: pd.DataFrame) -> Dict[date, List[datetime]]:
    """Return missing expected timestamps for each trading day in one contract."""
    if df.empty:
        return {}

    source = _standardize_frame(df)
    if "trading_day" not in source.columns:
        source["trading_day"] = _assign_trading_day(source["datetime"])

    missing: dict[date, list[datetime]] = {}
    for day_value, day_rows in source.groupby("trading_day"):
        trading_day = pd.Timestamp(day_value).date()
        expected = set(_expected_datetimes_for_day(trading_day))
        actual = set(
            pd.Timestamp(value).to_pydatetime()
            for value in day_rows["datetime"].dt.tz_localize(None).tolist()
        )
        missing[trading_day] = sorted(expected - actual)
    return missing


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


def _standardize_frame(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["symbol"] = result["symbol"].astype(str).str.upper()
    result["datetime"] = _to_shanghai_datetimes(result["datetime"])
    for column in PRICE_COLUMNS + ["volume", "open_interest"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def _to_shanghai_datetimes(values: pd.Series) -> pd.Series:
    timestamps = values.map(_to_shanghai_timestamp)
    return pd.Series(timestamps.to_list(), index=values.index)


def _to_shanghai_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(SHANGHAI_TZ)
    return timestamp.tz_convert(SHANGHAI_TZ)


def _assign_trading_day(datetimes: pd.Series) -> pd.Series:
    naive = pd.to_datetime(datetimes).dt.tz_localize(None)
    night = naive.dt.hour >= 21
    base = naive.dt.normalize()
    return base.where(~night, base + pd.Timedelta(days=1))


def _invalid_ohlc_mask(df: pd.DataFrame) -> pd.Series:
    return (
        (df["high"] < df[["open", "close"]].max(axis=1))
        | (df["low"] > df[["open", "close"]].min(axis=1))
        | (df["high"] < df["low"])
    )


def _limit_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    daily = (
        df.sort_values("datetime")
        .groupby(["symbol", "trading_day"], as_index=False)
        .agg(day_high=("high", "max"), day_low=("low", "min"), day_close=("close", "last"))
        .sort_values(["symbol", "trading_day"])
    )
    daily["previous_close"] = daily.groupby("symbol")["day_close"].shift(1)
    daily["limit_up_day"] = daily["day_high"] >= daily["previous_close"] * 1.10
    daily["limit_down_day"] = daily["day_low"] <= daily["previous_close"] * 0.90

    flags = daily.set_index(["symbol", "trading_day"])[
        ["limit_up_day", "limit_down_day"]
    ]
    index = pd.MultiIndex.from_frame(df[["symbol", "trading_day"]])
    limit_up = flags["limit_up_day"].reindex(index).fillna(False).to_numpy()
    limit_down = flags["limit_down_day"].reindex(index).fillna(False).to_numpy()
    return pd.Series(limit_up, index=df.index), pd.Series(limit_down, index=df.index)


def _flag_missing_neighbors(
    df: pd.DataFrame,
    missing: Dict[date, List[datetime]],
) -> None:
    missing_lookup = {
        day: set(values)
        for day, values in missing.items()
    }
    naive = df["datetime"].dt.tz_localize(None)
    for index, row in df[["trading_day"]].iterrows():
        trading_day = pd.Timestamp(row["trading_day"]).date()
        timestamp = naive.iloc[index].to_pydatetime()
        neighbors = {
            timestamp - timedelta(minutes=1),
            timestamp + timedelta(minutes=1),
        }
        if missing_lookup.get(trading_day, set()) & neighbors:
            df.at[index, "flags"] = int(df.at[index, "flags"]) | MISSING_NEIGHBOR


def _expected_datetimes_for_day(trading_day: date) -> list[datetime]:
    if trading_day.weekday() >= 5:
        return []

    previous = _previous_weekday(trading_day)
    expected: list[datetime] = []
    for minute in get_rb_standard_minutes(trading_day):
        calendar_day = previous if minute.hour >= 21 else trading_day
        expected.append(datetime.combine(calendar_day, minute))
    return expected


def _previous_weekday(value: date) -> date:
    previous = value - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous
