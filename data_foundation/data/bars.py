from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from .calendar import RBTradingCalendar


REQUIRED_COLUMNS = [
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "active_symbol",
    "flags",
]
BAR_COLUMNS = [
    "datetime",
    "trading_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "active_symbol",
    "flags",
]
AUDIT_COLUMNS = [
    "frequency_minutes",
    "trading_day",
    "session",
    "window_end",
    "expected_count",
    "actual_count",
    "status",
    "reason",
]


def aggregate_continuous_1m_to_5m(
    df_1m: pd.DataFrame,
    *,
    calendar: RBTradingCalendar | None = None,
    return_audit: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    return aggregate_continuous_1m_to_Nm(
        df_1m,
        5,
        calendar=calendar,
        return_audit=return_audit,
    )


def aggregate_continuous_1m_to_Nm(
    df_1m: pd.DataFrame,
    freq_minutes: int,
    *,
    calendar: RBTradingCalendar | None = None,
    return_audit: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate end-labelled minutes within calendar sessions."""
    if freq_minutes not in {5, 15, 30, 60}:
        raise ValueError("freq_minutes must be one of 5, 15, 30, 60")
    _require_columns(df_1m, REQUIRED_COLUMNS)
    if df_1m.empty:
        result = _empty_bar_frame()
        audit = pd.DataFrame(columns=AUDIT_COLUMNS)
        return (result, audit) if return_audit else result
    calendar = calendar or RBTradingCalendar.load_default()
    source = df_1m.copy()
    source["datetime"] = pd.to_datetime(source["datetime"])
    original_tz = getattr(source["datetime"].dt, "tz", None)
    mapped = calendar.map_datetimes(source["datetime"])
    if mapped["trading_day"].isna().any():
        bad = source.loc[mapped["trading_day"].isna(), "datetime"].iloc[0]
        raise ValueError(f"out-of-session minute cannot be aggregated: {bad}")
    source["trading_day"] = pd.to_datetime(mapped["trading_day"]).dt.normalize().to_numpy()
    source["session"] = mapped["session"].to_numpy()
    source["session_minute_index"] = mapped["session_minute_index"].astype(int).to_numpy()
    source["window_id"] = (source["session_minute_index"] - 1) // freq_minutes
    source["datetime_naive"] = source["datetime"]
    if original_tz is not None:
        source["datetime_naive"] = source["datetime"].dt.tz_localize(None)

    expected = pd.concat(
        [calendar.expected_minutes(pd.Timestamp(day)) for day in source["trading_day"].drop_duplicates()],
        ignore_index=True,
    )
    expected["window_id"] = (
        (expected["session_minute_index"].astype(int) - 1) // freq_minutes
    )
    keys = ["trading_day", "session", "window_id"]
    window_plan = (
        expected.groupby(keys, as_index=False)
        .agg(
            expected_count=("calendar_datetime", "size"),
            window_end=("calendar_datetime", "max"),
        )
    )
    grouped = (
        source.sort_values(["trading_day", "session", "session_minute_index"])
        .groupby(keys, as_index=False)
        .agg(
            actual_count=("datetime", "size"),
            actual_unique_count=("datetime", "nunique"),
            active_symbol_count=("active_symbol", lambda values: values.nunique(dropna=False)),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            open_interest=("open_interest", "last"),
            active_symbol=("active_symbol", "last"),
            flags=("flags", _bitwise_or),
        )
    )
    windows = window_plan.merge(grouped, on=keys, how="left")
    for column in ["actual_count", "actual_unique_count", "active_symbol_count"]:
        windows[column] = windows[column].fillna(0).astype(int)
    broken = windows[windows["active_symbol_count"] > 1]
    if not broken.empty:
        row = broken.iloc[0]
        raise ValueError(
            "ACTIVE_SYMBOL_INVARIANT_BROKEN: "
            f"{pd.Timestamp(row.trading_day).date()} {row.session} ending {row.window_end}"
        )
    tail = windows["expected_count"] < freq_minutes
    duplicate = windows["actual_count"] != windows["actual_unique_count"]
    missing = (
        (windows["actual_count"] != freq_minutes)
        | (windows["actual_unique_count"] != freq_minutes)
    )
    windows["status"] = "GENERATED"
    windows["reason"] = ""
    windows.loc[missing, ["status", "reason"]] = ["DROPPED", "MISSING_MINUTE"]
    windows.loc[duplicate, ["status", "reason"]] = ["DROPPED", "DUPLICATE_MINUTE"]
    windows.loc[tail, ["status", "reason"]] = ["DROPPED", "SESSION_TAIL"]
    complete = windows[windows["status"] == "GENERATED"].copy()
    complete["datetime"] = complete["window_end"]
    if original_tz is not None:
        complete["datetime"] = complete["datetime"].dt.tz_localize(original_tz)
    result = complete[BAR_COLUMNS].sort_values("datetime", kind="mergesort").reset_index(drop=True)
    audit = windows.assign(frequency_minutes=freq_minutes)[AUDIT_COLUMNS].copy()
    if original_tz is not None:
        audit["window_end"] = audit["window_end"].dt.tz_localize(original_tz)
    audit = audit.sort_values("window_end", kind="mergesort").reset_index(drop=True)
    return (result, audit) if return_audit else result


def _infer_trading_day(datetimes: pd.Series) -> pd.Series:
    """Compatibility helper backed by the static calendar."""
    calendar = RBTradingCalendar.load_default()
    return pd.to_datetime(calendar.map_datetimes(datetimes)["trading_day"])


def _bitwise_or(values: Iterable[object]) -> int:
    array = pd.Series(values).fillna(0).astype(int).to_numpy()
    return int(np.bitwise_or.reduce(array)) if len(array) else 0


def _require_columns(df: pd.DataFrame, required: list[str]) -> None:
    if missing := sorted(set(required) - set(df.columns)):
        raise ValueError(f"missing required columns: {missing}")


def _empty_bar_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLUMNS)
