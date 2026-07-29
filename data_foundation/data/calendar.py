from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd


STATIC_DIR = Path(__file__).with_name("static")
TRADING_DAYS_PATH = STATIC_DIR / "rb_trading_days.csv"
SESSION_RULES_PATH = STATIC_DIR / "session_rules.csv"
LIMIT_RULES_PATH = STATIC_DIR / "limit_rules.csv"


@dataclass
class RBTradingCalendar:
    """Repository-local SHFE/RB calendar and versioned session rules."""

    trading_days: pd.DataFrame
    session_rules: pd.DataFrame
    _expected_cache: dict[pd.Timestamp, pd.DataFrame] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def load_default(cls) -> "RBTradingCalendar":
        days = pd.read_csv(TRADING_DAYS_PATH, parse_dates=["trading_day", "night_session_date"])
        rules = pd.read_csv(SESSION_RULES_PATH, parse_dates=["valid_from", "valid_to"])
        days["trading_day"] = days["trading_day"].dt.normalize()
        days["night_session_date"] = days["night_session_date"].dt.normalize()
        days["has_night_session"] = days["has_night_session"].astype(bool)
        calendar = cls(days, rules)
        calendar.validate()
        return calendar

    @classmethod
    def from_frames(
        cls,
        trading_days: pd.DataFrame,
        session_rules: pd.DataFrame,
    ) -> "RBTradingCalendar":
        days = trading_days.copy()
        rules = session_rules.copy()
        for column in ["trading_day", "night_session_date"]:
            days[column] = pd.to_datetime(days[column]).dt.normalize()
        for column in ["valid_from", "valid_to"]:
            rules[column] = pd.to_datetime(rules[column]).dt.normalize()
        calendar = cls(days, rules)
        calendar.validate()
        return calendar

    @property
    def first_day(self) -> pd.Timestamp:
        return pd.Timestamp(self.trading_days["trading_day"].min())

    @property
    def last_day(self) -> pd.Timestamp:
        return pd.Timestamp(self.trading_days["trading_day"].max())

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
        candidate_days = self.trading_days[
            (self.trading_days["trading_day"] >= lower - pd.Timedelta(days=10))
            & (self.trading_days["trading_day"] <= upper + pd.Timedelta(days=10))
        ]["trading_day"]
        if candidate_days.empty:
            raise ValueError(
                f"timestamps outside calendar range {self.first_day.date()}..{self.last_day.date()}"
            )
        expected = pd.concat(
            [self.expected_minutes(day) for day in candidate_days], ignore_index=True
        )
        lookup = expected.set_index("calendar_datetime")
        mapped = lookup.reindex(pd.DatetimeIndex(naive))
        mapped.index = values.index
        return mapped

    def _ensure_in_range(self, day: pd.Timestamp) -> None:
        if day < self.first_day or day > self.last_day:
            raise ValueError(
                f"trading day {day.date()} outside calendar range "
                f"{self.first_day.date()}..{self.last_day.date()}"
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
