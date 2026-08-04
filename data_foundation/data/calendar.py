from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Any

import pandas as pd


STATIC_DIR = Path(__file__).with_name("static")
TRADING_DAYS_PATH = STATIC_DIR / "rb_trading_days.csv"
SESSION_RULES_PATH = STATIC_DIR / "session_rules.csv"
LIMIT_RULES_PATH = STATIC_DIR / "limit_rules.csv"
CALENDAR_METADATA_PATH = STATIC_DIR / "rb_calendar_metadata.json"


class CalendarCoverageError(ValueError):
    """Raised when runtime data falls outside the reviewed calendar horizon."""


@dataclass
class RBTradingCalendar:
    """Repository-local SHFE/RB calendar and versioned session rules."""

    trading_days: pd.DataFrame
    session_rules: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)
    _expected_cache: dict[pd.Timestamp, pd.DataFrame] = field(
        default_factory=dict, init=False, repr=False
    )
    _minute_lookup_cache: dict[
        pd.Timestamp, dict[pd.Timestamp, tuple[pd.Timestamp, str, int]]
    ] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def load_default(cls) -> "RBTradingCalendar":
        days = pd.read_csv(TRADING_DAYS_PATH, parse_dates=["trading_day", "night_session_date"])
        rules = pd.read_csv(SESSION_RULES_PATH, parse_dates=["valid_from", "valid_to"])
        metadata = json.loads(CALENDAR_METADATA_PATH.read_text(encoding="utf-8"))
        days["trading_day"] = days["trading_day"].dt.normalize()
        days["night_session_date"] = days["night_session_date"].dt.normalize()
        days["has_night_session"] = days["has_night_session"].astype(bool)
        calendar = cls(days, rules, metadata)
        calendar.validate()
        return calendar

    @classmethod
    def from_frames(
        cls,
        trading_days: pd.DataFrame,
        session_rules: pd.DataFrame,
        metadata: dict[str, Any] | None = None,
    ) -> "RBTradingCalendar":
        days = trading_days.copy()
        rules = session_rules.copy()
        for column in ["trading_day", "night_session_date"]:
            days[column] = pd.to_datetime(days[column]).dt.normalize()
        for column in ["valid_from", "valid_to"]:
            rules[column] = pd.to_datetime(rules[column]).dt.normalize()
        calendar = cls(days, rules, dict(metadata or {}))
        calendar.validate()
        return calendar

    @property
    def first_day(self) -> pd.Timestamp:
        return pd.Timestamp(self.trading_days["trading_day"].min())

    @property
    def last_day(self) -> pd.Timestamp:
        return pd.Timestamp(self.trading_days["trading_day"].max())

    @property
    def calendar_id(self) -> str:
        return str(self.metadata.get("calendar_id", ""))

    @property
    def valid_through(self) -> pd.Timestamp:
        value = self.metadata.get("valid_through")
        return pd.Timestamp(value).normalize() if value else self.last_day

    def validate(self) -> None:
        day_required = {
            "trading_day",
            "night_session_date",
            "has_night_session",
            "session_rule_id",
        }
        rule_required = {
            "rule_id",
            "valid_from",
            "valid_to",
            "session",
            "start_time",
            "end_time",
            "minute_count",
        }
        if missing := day_required - set(self.trading_days.columns):
            raise ValueError(f"calendar missing columns: {sorted(missing)}")
        if missing := rule_required - set(self.session_rules.columns):
            raise ValueError(f"session rules missing columns: {sorted(missing)}")
        if self.trading_days["trading_day"].duplicated().any():
            raise ValueError("duplicate trading_day in calendar")
        if self.metadata:
            if self.metadata.get("minute_label_convention") != "bar_end":
                raise ValueError("RB calendar must use bar_end minute labels")
            if self.valid_through != self.last_day:
                raise ValueError(
                    "calendar metadata valid_through does not match trading-day data"
                )
        for row in self.session_rules.itertuples(index=False):
            actual = len(_minute_times(_parse_time(row.start_time), _parse_time(row.end_time)))
            if actual != int(row.minute_count):
                raise ValueError(f"invalid minute_count for {row.rule_id}/{row.session}")

    def expected_minutes(self, trading_day: date | pd.Timestamp) -> pd.DataFrame:
        day = pd.Timestamp(trading_day).normalize()
        self._ensure_in_range(day)
        if day in self._expected_cache:
            return self._expected_cache[day].copy()
        matched = self.trading_days[self.trading_days["trading_day"] == day]
        if matched.empty:
            return _empty_expected_minutes()
        day_row = matched.iloc[0]
        rules = self.session_rules[
            (self.session_rules["rule_id"] == day_row["session_rule_id"])
            & (self.session_rules["valid_from"] <= day)
            & (self.session_rules["valid_to"] >= day)
        ]
        rows: list[dict[str, object]] = []
        for rule in rules.itertuples(index=False):
            if rule.session == "NIGHT" and not bool(day_row["has_night_session"]):
                continue
            calendar_day = (
                pd.Timestamp(day_row["night_session_date"])
                if rule.session == "NIGHT"
                else day
            )
            for index, minute in enumerate(
                _minute_times(_parse_time(rule.start_time), _parse_time(rule.end_time)),
                start=1,
            ):
                rows.append(
                    {
                        "trading_day": day,
                        "calendar_datetime": pd.Timestamp.combine(calendar_day.date(), minute),
                        "session": rule.session,
                        "session_minute_index": index,
                    }
                )
        result = pd.DataFrame(rows, columns=list(_empty_expected_minutes().columns))
        self._expected_cache[day] = result
        return result.copy()

    def expected_minutes_between(
        self,
        start: date | pd.Timestamp,
        end: date | pd.Timestamp,
    ) -> pd.DataFrame:
        start_day = pd.Timestamp(start).normalize()
        end_day = pd.Timestamp(end).normalize()
        self._ensure_in_range(start_day)
        self._ensure_in_range(end_day)
        days = self.trading_days.loc[
            self.trading_days["trading_day"].between(start_day, end_day),
            "trading_day",
        ]
        frames = [self.expected_minutes(day) for day in days]
        return pd.concat(frames, ignore_index=True) if frames else _empty_expected_minutes()

    def validate_coverage(
        self,
        start: date | pd.Timestamp,
        end: date | pd.Timestamp,
    ) -> None:
        """Validate an inclusive date span against the reviewed horizon."""
        start_day = pd.Timestamp(start).normalize()
        end_day = pd.Timestamp(end).normalize()
        if end_day < start_day:
            raise CalendarCoverageError(
                f"calendar_out_of_range: invalid range {start_day.date()}..{end_day.date()}"
            )
        if start_day < self.first_day or end_day > self.valid_through:
            raise CalendarCoverageError(
                "calendar_out_of_range: requested "
                f"{start_day.date()}..{end_day.date()}, reviewed "
                f"{self.first_day.date()}..{self.valid_through.date()}"
            )

    def next_trading_day(
        self,
        value: date | pd.Timestamp,
        *,
        inclusive: bool = False,
    ) -> pd.Timestamp:
        day = pd.Timestamp(value).normalize()
        mask = (
            self.trading_days["trading_day"].ge(day)
            if inclusive
            else self.trading_days["trading_day"].gt(day)
        )
        candidates = self.trading_days.loc[mask, "trading_day"]
        if candidates.empty:
            raise CalendarCoverageError(
                "calendar_out_of_range: no reviewed next trading day after "
                f"{day.date()} (valid_through={self.valid_through.date()})"
            )
        return pd.Timestamp(candidates.iloc[0]).normalize()

    def latest_complete_session_end(
        self,
        value: datetime | pd.Timestamp,
    ) -> pd.Timestamp:
        """Return the latest RB session end strictly before ``value``."""
        timestamp = _timestamp_naive(pd.Timestamp(value))
        self.validate_coverage(timestamp, timestamp)
        lower = max(self.first_day, timestamp.normalize() - pd.Timedelta(days=10))
        expected = self.expected_minutes_between(lower, timestamp.normalize())
        if expected.empty:
            raise CalendarCoverageError(
                f"calendar_out_of_range: no complete session before {timestamp}"
            )
        session_ends = expected.groupby(
            ["trading_day", "session"], as_index=False
        )["calendar_datetime"].max()
        completed = session_ends[
            session_ends["calendar_datetime"].map(_timestamp_naive) < timestamp
        ]
        if completed.empty:
            raise CalendarCoverageError(
                f"calendar_out_of_range: no complete session before {timestamp}"
            )
        return pd.Timestamp(completed["calendar_datetime"].max())

    def map_datetimes(self, values: pd.Series) -> pd.DataFrame:
        if values.empty:
            return pd.DataFrame(
                index=values.index,
                columns=["trading_day", "session", "session_minute_index"],
            )
        naive = pd.to_datetime(values)
        if getattr(naive.dt, "tz", None) is not None:
            naive = naive.dt.tz_localize(None)
        lower = naive.min().normalize()
        upper = naive.max().normalize()
        if lower < self.first_day - pd.Timedelta(days=3) or upper > self.valid_through:
            raise CalendarCoverageError(
                "calendar_out_of_range: timestamps requested for "
                f"{lower.date()}..{upper.date()}, reviewed through "
                f"{self.valid_through.date()}"
            )
        candidate_days = self.trading_days[
            (self.trading_days["trading_day"] >= lower - pd.Timedelta(days=10))
            & (self.trading_days["trading_day"] <= upper + pd.Timedelta(days=10))
        ]["trading_day"]
        if candidate_days.empty:
            raise CalendarCoverageError(
                f"timestamps outside calendar range {self.first_day.date()}..{self.last_day.date()}"
            )
        expected = pd.concat(
            [self.expected_minutes(day) for day in candidate_days], ignore_index=True
        )
        lookup = expected.set_index("calendar_datetime")
        mapped = lookup.reindex(pd.DatetimeIndex(naive))
        mapped.index = values.index
        return mapped

    def map_datetime(
        self,
        value: datetime | pd.Timestamp,
    ) -> tuple[pd.Timestamp, str, int] | None:
        """Map one minute using cached trading-day lookup tables."""
        timestamp = _timestamp_naive(pd.Timestamp(value))
        lower = timestamp.normalize()
        if lower < self.first_day - pd.Timedelta(days=3) or lower > self.valid_through:
            raise CalendarCoverageError(
                "calendar_out_of_range: timestamp requested for "
                f"{timestamp}, reviewed through {self.valid_through.date()}"
            )
        candidates = self.trading_days.loc[
            self.trading_days["trading_day"].between(
                lower,
                lower + pd.Timedelta(days=10),
            ),
            "trading_day",
        ]
        for value_day in candidates:
            trading_day = pd.Timestamp(value_day).normalize()
            lookup = self._minute_lookup_cache.get(trading_day)
            if lookup is None:
                expected = self.expected_minutes(trading_day)
                lookup = {
                    _timestamp_naive(pd.Timestamp(row.calendar_datetime)): (
                        trading_day,
                        str(row.session),
                        int(row.session_minute_index),
                    )
                    for row in expected.itertuples(index=False)
                }
                self._minute_lookup_cache[trading_day] = lookup
            mapped = lookup.get(timestamp)
            if mapped is not None:
                return mapped
        return None

    def _ensure_in_range(self, day: pd.Timestamp) -> None:
        if day < self.first_day or day > self.last_day:
            raise CalendarCoverageError(
                "calendar_out_of_range: trading day "
                f"{day.date()} outside calendar range "
                f"{self.first_day.date()}..{self.valid_through.date()}"
            )


def load_limit_rules(path: Path = LIMIT_RULES_PATH) -> pd.DataFrame:
    rules = pd.read_csv(path, parse_dates=["valid_from", "valid_to"])
    rules["valid_from"] = rules["valid_from"].dt.normalize()
    rules["valid_to"] = rules["valid_to"].dt.normalize()
    return rules


def _parse_time(value: object) -> time:
    return time.fromisoformat(str(value))


def _minute_times(start: time, end: time) -> list[time]:
    current = datetime.combine(date.min, start)
    stop = datetime.combine(date.min, end)
    result: list[time] = []
    while current <= stop:
        result.append(current.time())
        current += timedelta(minutes=1)
    return result


def _empty_expected_minutes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trading_day",
            "calendar_datetime",
            "session",
            "session_minute_index",
        ]
    )


def _timestamp_naive(value: pd.Timestamp) -> pd.Timestamp:
    if value.tzinfo is not None:
        return value.tz_convert("Asia/Shanghai").tz_localize(None)
    return value
