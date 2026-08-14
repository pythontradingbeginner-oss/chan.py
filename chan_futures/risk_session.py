"""Shared RB trading-session resolution for risk state transitions."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data_foundation import CalendarCoverageError, RBTradingCalendar


@dataclass(frozen=True, slots=True)
class RiskSessionContext:
    session_key: str
    observed_at: pd.Timestamp
    source: str
    market_session: str
    calendar_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_key": self.session_key,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "market_session": self.market_session,
            "calendar_id": self.calendar_id,
        }


class RbRiskSessionResolver:
    """Resolve bars and fills onto the repository-reviewed RB trading day."""

    def __init__(self, calendar: RBTradingCalendar | None = None) -> None:
        self.calendar = calendar or RBTradingCalendar.load_default()

    def resolve(
        self,
        timestamp: object,
        *,
        source: str,
        explicit_trading_day: object | None = None,
    ) -> RiskSessionContext:
        observed_at = pd.Timestamp(timestamp)
        lookup_time = observed_at.floor("min")
        mapped = self.calendar.map_datetime(lookup_time)
        if mapped is None:
            raise CalendarCoverageError(
                f"risk_session_unavailable: no RB trading session for {lookup_time}"
            )

        trading_day, market_session, _ = mapped
        session_key = pd.Timestamp(trading_day).date().isoformat()
        if explicit_trading_day is not None and not pd.isna(explicit_trading_day):
            explicit_key = pd.Timestamp(explicit_trading_day).date().isoformat()
            if explicit_key != session_key:
                raise CalendarCoverageError(
                    "risk_session_mismatch: "
                    f"explicit={explicit_key}, calendar={session_key}, "
                    f"observed_at={lookup_time}"
                )

        return RiskSessionContext(
            session_key=session_key,
            observed_at=observed_at,
            source=str(source),
            market_session=str(market_session),
            calendar_id=self.calendar.calendar_id,
        )
