"""P7.2 versioned unified runtime state package.

Wraps the CTA strategy's persisted state (position, structural stops, exit
state, risk counters, order machine, decision sequence, trade ids) under a
single version number.  A version bump invalidates older snapshots: the
strategy must not reuse state produced by an incompatible build, otherwise it
could silently resume on stale decisions.

Serialization format (compact JSON):

{
  "state_version": 1,
  "strategy_id": "chan.py|rb2610.SHFE",
  "persisted_at": "2026-08-04T...",
  "position": {...} | null,
  "exit_state": {...},
  "risk": {...},
  "orders": {...},
  "decision_ids": [...],
  "trade_ids": [...]
}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

STATE_VERSION = 1


class RuntimeStateVersionError(ValueError):
    """Raised when a persisted state package is from an incompatible build."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


@dataclass(slots=True)
class RuntimeStatePackage:
    strategy_id: str
    position: dict[str, Any] | None = None
    exit_state: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    orders: dict[str, Any] = field(default_factory=dict)
    decision_ids: list[str] = field(default_factory=list)
    trade_ids: list[str] = field(default_factory=list)
    persisted_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_version": STATE_VERSION,
            "strategy_id": self.strategy_id,
            "persisted_at": self.persisted_at,
            "position": self.position,
            "exit_state": self.exit_state,
            "risk": self.risk,
            "orders": self.orders,
            "decision_ids": self.decision_ids,
            "trade_ids": self.trade_ids,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, expected_strategy_id: str = "") -> "RuntimeStatePackage":
        version = int(data.get("state_version", -1))
        if version != STATE_VERSION:
            raise RuntimeStateVersionError(
                "state_version_mismatch",
                f"expected={STATE_VERSION} actual={version}",
            )
        strategy_id = str(data.get("strategy_id", ""))
        if expected_strategy_id and strategy_id != expected_strategy_id:
            raise RuntimeStateVersionError(
                "strategy_id_mismatch",
                f"expected={expected_strategy_id} actual={strategy_id}",
            )
        return cls(
            strategy_id=strategy_id,
            position=data.get("position"),
            exit_state=dict(data.get("exit_state") or {}),
            risk=dict(data.get("risk") or {}),
            orders=dict(data.get("orders") or {}),
            decision_ids=list(data.get("decision_ids") or []),
            trade_ids=list(data.get("trade_ids") or []),
            persisted_at=str(data.get("persisted_at") or ""),
        )

    @classmethod
    def from_json(cls, raw: str, *, expected_strategy_id: str = "") -> "RuntimeStatePackage":
        return cls.from_dict(
            json.loads(raw),
            expected_strategy_id=expected_strategy_id,
        )
