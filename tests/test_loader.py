from __future__ import annotations

import sqlite3

import pandas as pd

from data_foundation.data.loader import load_cleaned_1m, load_raw_from_vnpy


def test_load_raw_from_vnpy_maps_fields_from_sqlite(tmp_path):
    db_path = tmp_path / "database.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE DbBarData (
                symbol TEXT,
                exchange TEXT,
                datetime DATETIME,
                interval TEXT,
                volume REAL,
                turnover REAL,
                open_interest REAL,
                open_price REAL,
                high_price REAL,
                low_price REAL,
                close_price REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO DbBarData VALUES
            ('rb2501', 'SHFE', '2025-01-02 09:01:00', '1m',
             10, 1000, 200, 100, 101, 99, 100)
            """
        )

    frame = load_raw_from_vnpy(db_path)

    assert list(frame.columns) == [
        "symbol",
        "exchange",
        "datetime",
        "volume",
        "turnover",
        "open_interest",
        "open",
        "high",
        "low",
        "close",
    ]
    assert frame.iloc[0]["symbol"] == "RB2501"
    assert frame.iloc[0]["open"] == 100


def test_parquet_round_trip_for_cleaned_loader(tmp_path):
    path = tmp_path / "clean.parquet"
    expected = pd.DataFrame(
        {
            "symbol": ["RB2501"],
            "datetime": [pd.Timestamp("2025-01-02 09:01")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1.0],
            "open_interest": [100.0],
            "flags": [0],
        }
    )
    expected.to_parquet(path, compression="snappy", index=False)

    loaded = load_cleaned_1m(path)

    pd.testing.assert_frame_equal(loaded, expected)
