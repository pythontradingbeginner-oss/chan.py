"""Build the repository-local RB trading calendar.

This maintenance utility is not used at runtime.  It combines weekday dates
with the published Chinese statutory-holiday dataset exposed by the optional
``chinesecalendar`` package, then writes a reviewed static CSV.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from chinese_calendar import is_holiday


START = date(2018, 1, 15)
END = date(2025, 4, 3)
OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "data_foundation/data/static/rb_trading_days.csv"
)
EXCHANGE_SPECIFIC_CLOSURES = {
    # SHFE 2024 Spring Festival closure began one weekday before the statutory
    # public-holiday dataset's start date.
    date(2024, 2, 9),
}
NIGHT_SESSION_SUSPENSIONS = [
    # The Chinese futures exchanges suspended night trading during the initial
    # COVID-19 response. Night trading resumed on the evening of 2020-05-06,
    # which belongs to trading day 2020-05-07.
    (date(2020, 2, 3), date(2020, 5, 6)),
]


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
        has_night = not holiday_break and not suspended
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
    print(f"wrote {len(rows)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()

