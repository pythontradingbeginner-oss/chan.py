from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit


PRICE_COLUMNS = ("open", "high", "low", "close")


def row_to_klu(
    row: pd.Series | dict[str, Any],
    *,
    kl_type: KL_TYPE = KL_TYPE.K_1M,
    datetime_column: str = "datetime",
) -> CKLine_Unit:
    """Convert one futures OHLCV row into chan.py's CKLine_Unit."""
    item = {
        DATA_FIELD.FIELD_TIME: _to_ctime(row[datetime_column]),
        DATA_FIELD.FIELD_OPEN: float(row["open"]),
        DATA_FIELD.FIELD_HIGH: float(row["high"]),
        DATA_FIELD.FIELD_LOW: float(row["low"]),
        DATA_FIELD.FIELD_CLOSE: float(row["close"]),
    }
    _add_optional_float(item, row, "volume", DATA_FIELD.FIELD_VOLUME)
    _add_optional_float(item, row, "turnover", DATA_FIELD.FIELD_TURNOVER)
    klu = CKLine_Unit(item, autofix=True)
    klu.kl_type = kl_type
    return klu


def dataframe_to_klu_iter(
    frame: pd.DataFrame,
    *,
    kl_type: KL_TYPE = KL_TYPE.K_1M,
    datetime_column: str = "datetime",
) -> Iterator[CKLine_Unit]:
    """Yield monotonic CKLine_Unit objects from a futures bar DataFrame."""
    prepared = prepare_ohlc_frame(frame, datetime_column=datetime_column)
    for _, row in prepared.iterrows():
        yield row_to_klu(row, kl_type=kl_type, datetime_column=datetime_column)


def prepare_ohlc_frame(
    frame: pd.DataFrame,
    *,
    datetime_column: str = "datetime",
) -> pd.DataFrame:
    """Validate and sort a futures OHLC frame for chan.py replay."""
    missing = [column for column in (datetime_column, *PRICE_COLUMNS) if column not in frame]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    prepared = frame.copy()
    prepared[datetime_column] = pd.to_datetime(prepared[datetime_column])
    prepared = prepared.sort_values(datetime_column).drop_duplicates(datetime_column)
    prepared = prepared.reset_index(drop=True)
    return prepared


def _to_ctime(value: Any) -> CTime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return CTime(
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second,
        auto=False,
    )


def _add_optional_float(
    item: dict[str, Any],
    row: pd.Series | dict[str, Any],
    source_key: str,
    target_key: str,
) -> None:
    if source_key not in row:
        return
    value = row[source_key]
    if pd.isna(value):
        return
    item[target_key] = float(value)
