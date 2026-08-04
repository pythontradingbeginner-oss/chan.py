from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class WarmupReadiness:
    """Auditable snapshot produced after deterministic SQLite warmup."""

    input_1m_count: int = 0
    bar_5m_count: int = 0
    bar_15m_count: int = 0
    bar_60m_count: int = 0
    last_1m: datetime | None = None
    last_5m: datetime | None = None
    last_15m: datetime | None = None
    last_60m: datetime | None = None
    rejected_5m_count: int = 0
    rejected_15m_count: int = 0
    rejected_60m_count: int = 0
    fresh: bool = False
    sequence_valid: bool = False
    aggregation_aligned: bool = False
    main_confirmed_bis: int = 0
    child_confirmed_bis: int = 0
    parent_confirmed_segments: int = 0
    required_15m_bars: int = 800
    fresh_through: datetime | None = None
    reasons: tuple[str, ...] = ()

    @property
    def structure_ready(self) -> bool:
        return (
            self.main_confirmed_bis > 0
            and self.child_confirmed_bis > 0
            and self.parent_confirmed_segments > 0
        )

    @property
    def ready(self) -> bool:
        return not self.reasons

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in ("last_1m", "last_5m", "last_15m", "last_60m", "fresh_through"):
            value = payload[name]
            payload[name] = value.isoformat() if value is not None else None
        payload["reasons"] = list(self.reasons)
        payload["structure_ready"] = self.structure_ready
        payload["ready"] = self.ready
        return payload


class WarmupValidationError(ValueError):
    """Expected initialization failure that must leave the strategy blocked."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)
