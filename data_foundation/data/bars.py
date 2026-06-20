from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


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


def aggregate_continuous_1m_to_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate one-minute continuous RB bars into complete five-minute bars.

    Args:
        df_1m: Continuous one-minute bars. If trading_day is missing, it is
            inferred from Shanghai futures session timestamps.

    Returns:
        A new DataFrame containing complete five-minute bars only.
    """
    _require_columns(df_1m, REQUIRED_COLUMNS)
    if df_1m.empty:
        return _empty_5m_frame()

    source = df_1m.copy()
    source["datetime"] = pd.to_datetime(source["datetime"])
    if "trading_day" not in source.columns:
        source["trading_day"] = _infer_trading_day(source["datetime"])
    else:
        source["trading_day"] = pd.to_datetime(source["trading_day"]).dt.normalize()

    source = source.sort_values(["trading_day", "datetime"]).reset_index(drop=True)
    source["window_start"] = source["datetime"].dt.floor("5min")
    grouped = (
        source.groupby(["trading_day", "window_start"], sort=True)
        .agg(
            source_1m_count=("datetime", "size"),
            datetime=("window_start", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            open_interest=("open_interest", "last"),
            active_symbol=("active_symbol", "last"),
            flags=("flags", _bitwise_or),
        )
        .reset_index()
    )
    complete = grouped[grouped["source_1m_count"] == 5].copy()
    complete = complete.drop(columns=["source_1m_count"])
    columns = [
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
    complete["trading_day"] = pd.to_datetime(complete["trading_day"]).dt.normalize()
    return complete[columns].reset_index(drop=True)


def aggregate_continuous_1m_to_Nm(
    df_1m: pd.DataFrame,
    freq_minutes: int,
) -> pd.DataFrame:
    """Aggregate one-minute continuous bars into complete N-minute bars."""
    if freq_minutes <= 0:
        raise ValueError("freq_minutes must be positive")
    _require_columns(df_1m, REQUIRED_COLUMNS)
    if df_1m.empty:
        return _empty_5m_frame()

    source = df_1m.copy()
    source["datetime"] = pd.to_datetime(source["datetime"])
    if "trading_day" not in source.columns:
        source["trading_day"] = _infer_trading_day(source["datetime"])
    else:
        source["trading_day"] = pd.to_datetime(source["trading_day"]).dt.normalize()

    source = source.sort_values(["trading_day", "datetime"]).reset_index(drop=True)
    freq_str = f"{freq_minutes}min"
    source["window_start"] = source["datetime"].dt.floor(freq_str)
    grouped = (
        source.groupby(["trading_day", "window_start"], sort=True)
        .agg(
            source_1m_count=("datetime", "size"),
            datetime=("window_start", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            open_interest=("open_interest", "last"),
            active_symbol=("active_symbol", "last"),
            flags=("flags", _bitwise_or),
        )
        .reset_index()
    )
    complete = grouped[grouped["source_1m_count"] == freq_minutes].copy()
    complete = complete.drop(columns=["window_start", "source_1m_count"])
    columns = [
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
    complete["trading_day"] = pd.to_datetime(complete["trading_day"]).dt.normalize()
    return complete[columns].reset_index(drop=True)


def _infer_trading_day(datetimes: pd.Series) -> pd.Series:
    values = pd.to_datetime(datetimes)
    if getattr(values.dt, "tz", None) is not None:
        values = values.dt.tz_localize(None)
    base = values.dt.normalize()
    night = values.dt.hour >= 21
    return base.where(~night, base + pd.Timedelta(days=1))


def _bitwise_or(values: Iterable[object]) -> int:
    array = pd.Series(values).fillna(0).astype(int).to_numpy()
    if len(array) == 0:
        return 0
    return int(np.bitwise_or.reduce(array))


def _require_columns(df: pd.DataFrame, required: list[str]) -> None:
    missing = sorted(set(required) - set(df.columns))
    if missing:
        raise ValueError(f"missing required columns: {missing}")


def _empty_5m_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
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
    )
