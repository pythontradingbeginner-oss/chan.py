"""Build the repository-local RB trading calendar.

This maintenance utility is not used at runtime.  It combines weekday dates
with the published Chinese statutory-holiday dataset exposed by the optional
``chinesecalendar`` package, then writes a reviewed static CSV.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
from chinese_calendar import is_holiday


START = date(2018, 1, 15)
END = date(2026, 12, 31)
STATIC_DIR = (
    Path(__file__).resolve().parents[1]
    / "data_foundation/data/static"
)
OUTPUT = STATIC_DIR / "rb_trading_days.csv"
METADATA_OUTPUT = STATIC_DIR / "rb_calendar_metadata.json"
CALENDAR_ID = "SHFE_RB_END_LABEL_V1_2026"
SOURCE_URLS = [
    "https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html",
    "https://www.shfe.com.cn/services/calenderandholidays/holiday/",
]
EXCHANGE_SPECIFIC_CLOSURES = {
    # SHFE 2024 Spring Festival closure began one weekday before the statutory
    # public-holiday dataset's start date.
    date(2024, 2, 9),
    *pd.date_range("2025-01-01", "2025-01-01", freq="D").date,
    *pd.date_range("2025-01-28", "2025-02-04", freq="D").date,
    *pd.date_range("2025-04-04", "2025-04-06", freq="D").date,
    *pd.date_range("2025-05-01", "2025-05-05", freq="D").date,
    *pd.date_range("2025-05-31", "2025-06-02", freq="D").date,
    *pd.date_range("2025-10-01", "2025-10-08", freq="D").date,
    *pd.date_range("2026-01-01", "2026-01-03", freq="D").date,
    *pd.date_range("2026-02-15", "2026-02-23", freq="D").date,
    *pd.date_range("2026-04-04", "2026-04-06", freq="D").date,
    *pd.date_range("2026-05-01", "2026-05-05", freq="D").date,
    *pd.date_range("2026-06-19", "2026-06-21", freq="D").date,
    *pd.date_range("2026-09-25", "2026-09-27", freq="D").date,
    *pd.date_range("2026-10-01", "2026-10-07", freq="D").date,
}
NIGHT_SESSION_SUSPENSIONS = [
    # The Chinese futures exchanges suspended night trading during the initial
    # COVID-19 response. Night trading resumed on the evening of 2020-05-06,
    # which belongs to trading day 2020-05-07.
    (date(2020, 2, 3), date(2020, 5, 6)),
]
NO_NIGHT_SESSION_DATES = {
    date(2024, 12, 31),
    date(2025, 1, 27),
    date(2025, 4, 3),
    date(2025, 4, 30),
    date(2025, 5, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 2, 13),
    date(2026, 4, 3),
    date(2026, 4, 30),
    date(2026, 6, 18),
    date(2026, 9, 24),
    date(2026, 9, 30),
}


def main() -> None:
    scan_start = START - timedelta(days=10)
    dates = pd.date_range(scan_start, END, freq="D")
    trading_days = [
        stamp.date()
        for stamp in dates
        if stamp.weekday() < 5
        and not is_holiday(stamp.date())
        and stamp.date() not in EXCHANGE_SPECIFIC_CLOSURES
    ]
    rows: list[dict[str, object]] = []
    for index, trading_day in enumerate(trading_days):
        if trading_day < START or index == 0:
            continue
        previous = trading_days[index - 1]
        between = [
            previous + timedelta(days=offset)
            for offset in range(1, (trading_day - previous).days)
        ]
        holiday_break = any(day.weekday() < 5 and is_holiday(day) for day in between)
        suspended = any(start <= trading_day <= end for start, end in NIGHT_SESSION_SUSPENSIONS)
        has_night = (
            not holiday_break
            and not suspended
            and previous not in NO_NIGHT_SESSION_DATES
        )
        rows.append(
            {
                "trading_day": trading_day.isoformat(),
                "night_session_date": previous.isoformat() if has_night else "",
                "has_night_session": has_night,
                "session_rule_id": "RB_V1",
                "source_note": "China holidays plus reviewed SHFE closures/night-session suspension",
            }
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUTPUT, index=False)
    metadata = {
        "calendar_id": CALENDAR_ID,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "valid_from": START.isoformat(),
        "valid_through": END.isoformat(),
        "minute_label_convention": "bar_end",
        "timezone": "Asia/Shanghai",
        "session_rule_id": "RB_V1",
        "source_urls": SOURCE_URLS,
        "review_required": "Verify SHFE special closures before extending valid_through.",
    }
    METADATA_OUTPUT.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} rows to {OUTPUT}")
    print(f"wrote metadata to {METADATA_OUTPUT}")


if __name__ == "__main__":
    main()

