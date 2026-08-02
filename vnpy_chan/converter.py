from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit


WINDOW_KL_TYPE_MAP: dict[int, KL_TYPE] = {
    1: KL_TYPE.K_1M,
    5: KL_TYPE.K_5M,
    15: KL_TYPE.K_15M,
    30: KL_TYPE.K_30M,
    60: KL_TYPE.K_60M,
}


def window_to_kl_type(window: int) -> KL_TYPE:
    """Map chan.py aggregation window minutes to chan.py K-line type."""
    try:
        return WINDOW_KL_TYPE_MAP[int(window)]
    except (KeyError, TypeError, ValueError) as exc:
        supported = ", ".join(str(value) for value in sorted(WINDOW_KL_TYPE_MAP))
        raise ValueError(f"unsupported chan.py window: {window}; supported: {supported}") from exc


def bar_to_klu(bar: Any, kl_type: KL_TYPE | None = None) -> CKLine_Unit:
    """Convert a vn.py BarData-like object into chan.py's CKLine_Unit."""
    item = {
        DATA_FIELD.FIELD_TIME: _to_ctime(bar.datetime),
        DATA_FIELD.FIELD_OPEN: float(bar.open_price),
        DATA_FIELD.FIELD_HIGH: float(bar.high_price),
        DATA_FIELD.FIELD_LOW: float(bar.low_price),
        DATA_FIELD.FIELD_CLOSE: float(bar.close_price),
    }
    _add_optional_attr(item, bar, "volume", DATA_FIELD.FIELD_VOLUME)
    _add_optional_attr(item, bar, "turnover", DATA_FIELD.FIELD_TURNOVER)
    klu = CKLine_Unit(item, autofix=True)
    klu.kl_type = kl_type or KL_TYPE.K_1M
    return klu


def bars_to_ohlc_frame(bars: Iterable[Any]) -> pd.DataFrame:
    """Convert vn.py BarData-like objects into the OHLC frame used by chan_futures."""
    rows: list[dict[str, object]] = []
    for bar in bars:
        rows.append(
            {
                "symbol": getattr(bar, "symbol", None),
                "exchange": _enum_value(getattr(bar, "exchange", None)),
                "datetime": pd.Timestamp(bar.datetime),
                "open": float(bar.open_price),
                "high": float(bar.high_price),
                "low": float(bar.low_price),
                "close": float(bar.close_price),
                "volume": float(getattr(bar, "volume", 0) or 0),
                "turnover": float(getattr(bar, "turnover", 0) or 0),
                "open_interest": float(getattr(bar, "open_interest", 0) or 0),
                "active_symbol": getattr(bar, "symbol", None),
                "flags": 0,
            }
        )
    columns = [
        "symbol",
        "exchange",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "open_interest",
        "active_symbol",
        "flags",
    ]
    return pd.DataFrame(rows, columns=columns)


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


def _add_optional_attr(
    item: dict[str, Any],
    source: Any,
    source_name: str,
    target_key: str,
) -> None:
    value = getattr(source, source_name, None)
    if value is None or pd.isna(value):
        return
    item[target_key] = float(value)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))
