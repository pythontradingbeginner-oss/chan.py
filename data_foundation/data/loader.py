from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
import warnings

import pandas as pd


DEFAULT_DB_PATH = Path(r"C:\Users\Administrator\.vntrader\database.db")
DEFAULT_CLEANED_PATH = Path("data/processed/RB_1m_raw_clean.parquet")
DEFAULT_CONTINUOUS_PATH = Path("data/processed/RB_1m_continuous.parquet")
DEFAULT_CONTINUOUS_RAW_PATH = Path("data/processed/RB_1m_continuous_raw.parquet")
DEFAULT_CONTINUOUS_ADJUSTED_PATH = Path("data/processed/RB_1m_continuous_adjusted.parquet")
DEFAULT_SWITCH_LOG_PATH = Path("data/processed/RB_main_switch_log.csv")


def load_raw_from_vnpy(
    db_path: Path = DEFAULT_DB_PATH,
    symbol_pattern: str = "RB%",
    exchange: str = "SHFE",
    interval: str = "1m",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> pd.DataFrame:
    """Load RB one-minute bars from a vn.py SQLite database.

    Args:
        db_path: Path to vn.py SQLite database.db.
        symbol_pattern: SQL LIKE pattern for symbols.
        exchange: Exchange code.
        interval: Bar interval.
        start: Optional inclusive start datetime.
        end: Optional inclusive end datetime.

    Returns:
        A DataFrame using the standard cleaning column names.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")

    query = [
        """
        SELECT
            upper(symbol) AS symbol,
            exchange,
            datetime,
            volume,
            turnover,
            open_interest,
            open_price AS open,
            high_price AS high,
            low_price AS low,
            close_price AS close
        FROM DbBarData
        WHERE upper(symbol) LIKE upper(?)
          AND exchange = ?
          AND interval = ?
        """
    ]
    params: list[object] = [symbol_pattern, exchange, interval]
    if start is not None:
        query.append("AND datetime >= ?")
        params.append(start)
    if end is not None:
        query.append("AND datetime <= ?")
        params.append(end)
    query.append("ORDER BY datetime, symbol")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        frame = pd.read_sql_query(
            "\n".join(query),
            connection,
            params=params,
            parse_dates=["datetime"],
        )
    return frame


def load_cleaned_1m(
    path: Path = DEFAULT_CLEANED_PATH,
) -> pd.DataFrame:
    """Load cleaned one-minute multi-contract RB bars."""
    return pd.read_parquet(path)


def load_continuous_1m(
    price_mode: Literal["raw", "adjusted"] | Path = "raw",
    path: Path | None = None,
) -> pd.DataFrame:
    """Load raw continuous prices by default, or the research-only adjusted series.

    Passing a Path as the first positional argument remains supported during
    migration and emits a FutureWarning.
    """
    if isinstance(price_mode, Path):
        if path is not None:
            raise ValueError("path specified twice")
        warnings.warn(
            "Passing path positionally is deprecated; use path=...",
            FutureWarning,
            stacklevel=2,
        )
        path = price_mode
        price_mode = "raw"
    if price_mode not in {"raw", "adjusted"}:
        raise ValueError("price_mode must be 'raw' or 'adjusted'")
    selected_path = path or (
        DEFAULT_CONTINUOUS_RAW_PATH
        if price_mode == "raw"
        else DEFAULT_CONTINUOUS_ADJUSTED_PATH
    )
    if path is None and not selected_path.exists() and DEFAULT_CONTINUOUS_PATH.exists():
        warnings.warn(
            "Using legacy RB_1m_continuous.parquet; rebuild continuous products",
            FutureWarning,
            stacklevel=2,
        )
        selected_path = DEFAULT_CONTINUOUS_PATH
    return pd.read_parquet(selected_path)


def load_switch_log(
    path: Path = DEFAULT_SWITCH_LOG_PATH,
) -> pd.DataFrame:
    """Load the RB main-contract switch log."""
    return pd.read_csv(
        path,
        parse_dates=["switch_date", "decision_date", "basis_date"],
    )
