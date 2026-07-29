from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from .calendar import RBTradingCalendar, load_limit_rules


EXTREME_MOVE = 1
EST_LIMIT_UP_TOUCH = 2
EST_LIMIT_DOWN_TOUCH = 4
LIMIT_UP = EST_LIMIT_UP_TOUCH  # Deprecated compatibility alias.
LIMIT_DOWN = EST_LIMIT_DOWN_TOUCH  # Deprecated compatibility alias.
MISSING_NEIGHBOR = 8
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRICE_COLUMNS = ["open", "high", "low", "close"]
MISSING_COLUMNS = [
    "symbol",
    "trading_day",
    "session",
    "missing_datetime",
    "expected_index",
    "reason",
]


@dataclass
class CleaningLog:
    """Audit counters and detail tables from the RB one-minute cleaner."""

    raw_rows: int
    deduplicated: int
    invalid_price_removed: int
    invalid_ohlc_removed: int
    extreme_moves_flagged: int
    limit_up_flagged: int
    limit_down_flagged: int
    missing_bars_per_day: Dict[date, int]
    final_rows: int
    affected_contract_days: int = 0
    duplicate_minutes: int = 0
    out_of_session_removed: int = 0
    missing_details: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=MISSING_COLUMNS), repr=False
    )
    contract_day_summary: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    out_of_session_details: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)

    def to_dict(self) -> dict:
        return {
            "raw_rows": self.raw_rows,
            "deduplicated": self.deduplicated,
            "invalid_price_removed": self.invalid_price_removed,
            "invalid_ohlc_removed": self.invalid_ohlc_removed,
            "extreme_moves_flagged": self.extreme_moves_flagged,
            "estimated_limit_up_touches": self.limit_up_flagged,
            "estimated_limit_down_touches": self.limit_down_flagged,
            "expected_bars_absent": len(self.missing_details),
            "affected_contract_days": self.affected_contract_days,
            "duplicate_minutes": self.duplicate_minutes,
            "out_of_session_removed": self.out_of_session_removed,
            "missing_bars_per_day": {
                key.isoformat(): value for key, value in self.missing_bars_per_day.items()
            },
            "final_rows": self.final_rows,
        }

    def to_markdown(self) -> str:
        rows = [
            ("Raw rows", self.raw_rows),
            ("Duplicates removed", self.deduplicated),
            ("Invalid price rows removed", self.invalid_price_removed),
            ("Invalid OHLC rows removed", self.invalid_ohlc_removed),
            ("Out-of-session rows removed", self.out_of_session_removed),
            ("Extreme moves flagged", self.extreme_moves_flagged),
            ("Estimated limit-up touches", self.limit_up_flagged),
            ("Estimated limit-down touches", self.limit_down_flagged),
            ("Expected bars absent", len(self.missing_details)),
            ("Affected contract days", self.affected_contract_days),
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
    calendar: RBTradingCalendar | None = None,
    limit_ratio: float | None = None,
) -> Tuple[pd.DataFrame, CleaningLog]:
    """Clean multi-contract RB one-minute bars using the static session calendar."""
    _require_columns(raw_df)
    if extreme_return_threshold <= 0:
        raise ValueError("extreme_return_threshold must be positive")
    calendar = calendar or RBTradingCalendar.load_default()

    raw_rows = len(raw_df)
    df = _standardize_frame(raw_df)
    duplicate_mask = df.duplicated(["symbol", "datetime"], keep="last")
    deduplicated = int(duplicate_mask.sum())
    df = df.loc[~duplicate_mask].copy()

    invalid_price_mask = (df[PRICE_COLUMNS] <= 0).any(axis=1)
    invalid_price_removed = int(invalid_price_mask.sum())
    df = df.loc[~invalid_price_mask].copy()

    invalid_ohlc_mask = _invalid_ohlc_mask(df)
    invalid_ohlc_removed = int(invalid_ohlc_mask.sum()) if drop_invalid_ohlc else 0
    if drop_invalid_ohlc:
        df = df.loc[~invalid_ohlc_mask].copy()

    mapped = calendar.map_datetimes(df["datetime"])
    out_mask = mapped["trading_day"].isna()
    out_details = df.loc[out_mask, ["symbol", "datetime"]].copy()
    out_details["reason"] = "OUT_OF_SESSION"
    df = df.loc[~out_mask].copy()
    mapped = mapped.loc[~out_mask]
    df["trading_day"] = pd.to_datetime(mapped["trading_day"]).dt.normalize().to_numpy()
    df["session"] = mapped["session"].to_numpy()
    df["session_minute_index"] = mapped["session_minute_index"].astype(int).to_numpy()
    df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)
    df["flags"] = 0

    returns = df.groupby("symbol")["close"].pct_change()
    extreme_mask = returns.abs() > extreme_return_threshold
    df.loc[extreme_mask.fillna(False), "flags"] |= EXTREME_MOVE

    df, limit_up_mask, limit_down_mask = _attach_estimated_limit_touches(
        df, limit_ratio=limit_ratio
    )
    df.loc[limit_up_mask, "flags"] |= EST_LIMIT_UP_TOUCH
    df.loc[limit_down_mask, "flags"] |= EST_LIMIT_DOWN_TOUCH

    missing = detect_missing_minutes(df, calendar=calendar)
    _flag_missing_neighbors(df, missing)
    summary = _contract_day_summary(df, missing)
    per_day = (
        missing.groupby("trading_day").size().astype(int).to_dict()
        if not missing.empty
        else {}
    )
    affected = (
        missing[["symbol", "trading_day"]].drop_duplicates().shape[0]
        if not missing.empty
        else 0
    )
    log = CleaningLog(
        raw_rows=raw_rows,
        deduplicated=deduplicated,
        invalid_price_removed=invalid_price_removed,
        invalid_ohlc_removed=invalid_ohlc_removed,
        extreme_moves_flagged=int(extreme_mask.fillna(False).sum()),
        limit_up_flagged=int(limit_up_mask.sum()),
        limit_down_flagged=int(limit_down_mask.sum()),
        missing_bars_per_day={pd.Timestamp(k).date(): v for k, v in per_day.items()},
        final_rows=len(df),
        affected_contract_days=affected,
        duplicate_minutes=deduplicated,
        out_of_session_removed=len(out_details),
        missing_details=missing,
        contract_day_summary=summary,
        out_of_session_details=out_details,
    )
    return df.reset_index(drop=True), log


def get_rb_standard_minutes(
    trading_date: date,
    calendar: RBTradingCalendar | None = None,
) -> List[time]:
    calendar = calendar or RBTradingCalendar.load_default()
    expected = calendar.expected_minutes(trading_date)
    return [pd.Timestamp(value).time() for value in expected["calendar_datetime"]]


def detect_missing_minutes(
    df: pd.DataFrame,
    calendar: RBTradingCalendar | None = None,
) -> pd.DataFrame:
    """Return absent expected bars independently by symbol/day/session."""
    if df.empty:
        return pd.DataFrame(columns=MISSING_COLUMNS)
    calendar = calendar or RBTradingCalendar.load_default()
    source = _standardize_frame(df)
    if not {"trading_day", "session", "session_minute_index"}.issubset(source.columns):
        mapped = calendar.map_datetimes(source["datetime"])
        source["trading_day"] = mapped["trading_day"]
        source["session"] = mapped["session"]
        source["session_minute_index"] = mapped["session_minute_index"]
    source = source.dropna(subset=["trading_day", "session"])
    days = pd.to_datetime(source["trading_day"].drop_duplicates())
    expected = pd.concat(
        [calendar.expected_minutes(day) for day in days], ignore_index=True
    )
    groups = source[["symbol", "trading_day", "session"]].drop_duplicates()
    candidates = groups.merge(expected, on=["trading_day", "session"], how="inner")
    actual = source[["symbol", "trading_day", "session", "datetime"]].drop_duplicates()
    actual["calendar_datetime"] = actual["datetime"].dt.tz_localize(None)
    actual = actual.drop(columns="datetime")
    compared = candidates.merge(
        actual.assign(_present=True),
        on=["symbol", "trading_day", "session", "calendar_datetime"],
        how="left",
    )
    missing = compared[compared["_present"].isna()].copy()
    missing = missing.rename(
        columns={
            "calendar_datetime": "missing_datetime",
            "session_minute_index": "expected_index",
        }
    )
    missing["reason"] = "EXPECTED_BAR_ABSENT"
    return missing[MISSING_COLUMNS].sort_values(
        ["symbol", "trading_day", "session", "missing_datetime"]
    ).reset_index(drop=True)


def _require_columns(df: pd.DataFrame) -> None:
    required = {
        "symbol", "datetime", "open", "high", "low", "close", "volume", "open_interest"
    }
    if missing := sorted(required - set(df.columns)):
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


def _invalid_ohlc_mask(df: pd.DataFrame) -> pd.Series:
    return (
        (df["high"] < df[["open", "close"]].max(axis=1))
        | (df["low"] > df[["open", "close"]].min(axis=1))
        | (df["high"] < df["low"])
    )


def _attach_estimated_limit_touches(
    df: pd.DataFrame,
    *,
    limit_ratio: float | None,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    result = df.copy()
    if result.empty:
        for column in [
            "proxy_previous_close",
            "limit_ratio",
            "estimated_limit_up",
            "estimated_limit_down",
            "limit_rule_source",
        ]:
            result[column] = pd.Series(dtype="object")
        empty = pd.Series(False, index=result.index, dtype=bool)
        return result, empty, empty.copy()
    daily_close = (
        result.sort_values("datetime")
        .groupby(["symbol", "trading_day"], as_index=False)
        .agg(day_close=("close", "last"))
        .sort_values(["symbol", "trading_day"])
    )
    daily_close["proxy_previous_close"] = daily_close.groupby("symbol")["day_close"].shift(1)
    if limit_ratio is not None:
        if limit_ratio <= 0:
            raise ValueError("limit_ratio must be positive")
        daily_close["limit_ratio"] = float(limit_ratio)
        daily_close["limit_rule_source"] = "explicit configuration"
    else:
        rules = load_limit_rules()
        daily_close["limit_ratio"] = pd.NA
        daily_close["limit_rule_source"] = pd.NA
        for rule in rules.itertuples(index=False):
            mask = daily_close["trading_day"].between(rule.valid_from, rule.valid_to)
            daily_close.loc[mask, "limit_ratio"] = float(rule.limit_ratio)
            daily_close.loc[mask, "limit_rule_source"] = str(rule.source_note)
    if daily_close["limit_ratio"].isna().any():
        bad = daily_close.loc[daily_close["limit_ratio"].isna(), "trading_day"].iloc[0]
        raise ValueError(f"no estimated limit rule for {pd.Timestamp(bad).date()}")
    daily_close["estimated_limit_up"] = daily_close["proxy_previous_close"] * (
        1 + daily_close["limit_ratio"].astype(float)
    )
    daily_close["estimated_limit_down"] = daily_close["proxy_previous_close"] * (
        1 - daily_close["limit_ratio"].astype(float)
    )
    metadata = daily_close.set_index(["symbol", "trading_day"])[
        [
            "proxy_previous_close",
            "limit_ratio",
            "estimated_limit_up",
            "estimated_limit_down",
            "limit_rule_source",
        ]
    ]
    index = pd.MultiIndex.from_frame(result[["symbol", "trading_day"]])
    attached = metadata.reindex(index).reset_index(drop=True)
    for column in attached.columns:
        result[column] = attached[column].to_numpy()
    up = result["high"] >= result["estimated_limit_up"]
    down = result["low"] <= result["estimated_limit_down"]
    return result, up.fillna(False), down.fillna(False)


def _flag_missing_neighbors(df: pd.DataFrame, missing: pd.DataFrame) -> None:
    if missing.empty:
        return
    targets = []
    for offset in (-1, 1):
        frame = missing[["symbol", "trading_day", "session", "missing_datetime"]].copy()
        frame["datetime_naive"] = frame["missing_datetime"] + pd.Timedelta(minutes=offset)
        targets.append(frame[["symbol", "trading_day", "session", "datetime_naive"]])
    lookup = pd.concat(targets, ignore_index=True).drop_duplicates()
    current = df[["symbol", "trading_day", "session", "datetime"]].copy()
    current["datetime_naive"] = current["datetime"].dt.tz_localize(None)
    marked = current.merge(
        lookup.assign(_missing_neighbor=True),
        on=["symbol", "trading_day", "session", "datetime_naive"],
        how="left",
    )["_missing_neighbor"].notna()
    df.loc[marked.to_numpy(), "flags"] |= MISSING_NEIGHBOR


def _contract_day_summary(df: pd.DataFrame, missing: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "symbol",
        "trading_day",
        "session",
        "actual_unique_minutes",
        "expected_bars_absent",
        "expected_minutes",
        "absence_rate",
    ]
    if df.empty:
        return pd.DataFrame(columns=columns)
    actual = (
        df.groupby(["symbol", "trading_day", "session"], as_index=False)
        .agg(actual_unique_minutes=("datetime", "nunique"))
    )
    absent = (
        missing.groupby(["symbol", "trading_day", "session"], as_index=False)
        .size()
        .rename(columns={"size": "expected_bars_absent"})
        if not missing.empty
        else pd.DataFrame(columns=["symbol", "trading_day", "session", "expected_bars_absent"])
    )
    summary = actual.merge(absent, on=["symbol", "trading_day", "session"], how="left")
    summary["expected_bars_absent"] = summary["expected_bars_absent"].fillna(0).astype(int)
    summary["expected_minutes"] = (
        summary["actual_unique_minutes"] + summary["expected_bars_absent"]
    )
    summary["absence_rate"] = summary["expected_bars_absent"] / summary["expected_minutes"]
    return summary[columns]
