"""P7.2/P7.3 versioned unified runtime state package.

Wraps the CTA strategy's persisted state (position, structural stops, exit
state, risk counters, order machine, decision sequence, trade ids) under a
single version number.  A version bump invalidates older snapshots: the
strategy must not reuse state produced by an incompatible build, otherwise it
could silently resume on stale decisions.

P7-R3 v2 adds:
  - strategy_name / vt_symbol / config_sha256 identity (config change forces
    recovery instead of resuming on stale anchors)
  - exit_state fully restored (bars_since_entry / MFE / MAE / structure stops)
  - decision_ids / trade_ids restored, plus vt_tradeids (idempotent fill keys)

Serialization format (compact JSON):

{
  "state_version": 2,
  "strategy_name": "chan.py",
  "vt_symbol": "rb2610.SHFE",
  "config_sha256": "...",
  "persisted_at": "2026-08-04T...",
  "position": {...} | null,
  "exit_state": {...},
  "risk": {...},
  "orders": {...},
  "decision_ids": [...],
  "trade_ids": [...],
  "vt_tradeids": [...]
}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

STATE_VERSION = 2


class RuntimeStateVersionError(ValueError):
    """Raised when a persisted state package is from an incompatible build."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


@dataclass(slots=True)
class RuntimeStatePackage:
    strategy_name: str = ""
    vt_symbol: str = ""
    config_sha256: str = ""
    position: dict[str, Any] | None = None
    exit_state: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    orders: dict[str, Any] = field(default_factory=dict)
    decision_ids: list[str] = field(default_factory=list)
    trade_ids: list[str] = field(default_factory=list)
    vt_tradeids: list[str] = field(default_factory=list)
    persisted_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_version": STATE_VERSION,
            "strategy_name": self.strategy_name,
            "vt_symbol": self.vt_symbol,
            "config_sha256": self.config_sha256,
            "persisted_at": self.persisted_at,
            "position": self.position,
            "exit_state": self.exit_state,
            "risk": self.risk,
            "orders": self.orders,
            "decision_ids": self.decision_ids,
            "trade_ids": self.trade_ids,
            "vt_tradeids": self.vt_tradeids,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        expected_strategy_name: str = "",
        expected_vt_symbol: str = "",
        expected_config_sha256: str = "",
    ) -> "RuntimeStatePackage":
        version = int(data.get("state_version", -1))
        if version != STATE_VERSION:
            raise RuntimeStateVersionError(
                "state_version_mismatch",
                f"expected={STATE_VERSION} actual={version}",
            )
        strategy_name = str(data.get("strategy_name", ""))
        if expected_strategy_name and strategy_name != expected_strategy_name:
            raise RuntimeStateVersionError(
                "strategy_name_mismatch",
                f"expected={expected_strategy_name} actual={strategy_name}",
            )
        vt_symbol = str(data.get("vt_symbol", ""))
        if expected_vt_symbol and vt_symbol != expected_vt_symbol:
            raise RuntimeStateVersionError(
                "vt_symbol_mismatch",
                f"expected={expected_vt_symbol} actual={vt_symbol}",
            )
        config_sha256 = str(data.get("config_sha256", ""))
        if expected_config_sha256 and config_sha256 != expected_config_sha256:
            raise RuntimeStateVersionError(
                "config_sha256_mismatch",
                f"expected={expected_config_sha256} actual={config_sha256}",
            )
        return cls(
            strategy_name=strategy_name,
            vt_symbol=vt_symbol,
            config_sha256=config_sha256,
            position=data.get("position"),
            exit_state=dict(data.get("exit_state") or {}),
            risk=dict(data.get("risk") or {}),
            orders=dict(data.get("orders") or {}),
            decision_ids=list(data.get("decision_ids") or []),
            trade_ids=list(data.get("trade_ids") or []),
            vt_tradeids=list(data.get("vt_tradeids") or []),
            persisted_at=str(data.get("persisted_at") or ""),
        )

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        expected_strategy_name: str = "",
        expected_vt_symbol: str = "",
        expected_config_sha256: str = "",
    ) -> "RuntimeStatePackage":
        return cls.from_dict(
            json.loads(raw),
            expected_strategy_name=expected_strategy_name,
            expected_vt_symbol=expected_vt_symbol,
            expected_config_sha256=expected_config_sha256,
        )
