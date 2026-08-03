from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


TERMINAL_STATUSES = {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED"}


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(slots=True)
class CtaTrackedOrder:
    vt_orderid: str
    role: str
    price: float = 0.0
    volume: float = 0.0
    traded: float = 0.0
    status: str = "SUBMITTING"
    reason: str = ""
    created_at: str = field(default_factory=_now_text)
    updated_at: str = field(default_factory=_now_text)

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL_STATUSES


class CtaOrderStatusMachine:
    """Minimal CTA order state machine with JSON persistence."""

    def __init__(self) -> None:
        self.active: dict[str, CtaTrackedOrder] = {}
        self.history: list[CtaTrackedOrder] = []

    @property
    def active_count(self) -> int:
        return len(self.active)

    @property
    def summary(self) -> str:
        if not self.active:
            return "idle"
        counts: dict[str, int] = {}
        for order in self.active.values():
            counts[order.status] = counts.get(order.status, 0) + 1
        return ",".join(f"{key}:{counts[key]}" for key in sorted(counts))

    def submit(
        self,
        vt_orderids: list[str],
        *,
        role: str,
        price: float,
        volume: float,
        reason: str = "",
    ) -> None:
        for vt_orderid in vt_orderids:
            self.active[str(vt_orderid)] = CtaTrackedOrder(
                vt_orderid=str(vt_orderid),
                role=role,
                price=float(price),
                volume=float(volume),
                reason=reason,
            )

    def on_order(self, order: Any) -> CtaTrackedOrder:
        vt_orderid = str(getattr(order, "vt_orderid", ""))
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            tracked = CtaTrackedOrder(vt_orderid=vt_orderid, role="recovered")
            self.active[vt_orderid] = tracked
        tracked.price = float(getattr(order, "price", tracked.price) or 0)
        tracked.volume = float(getattr(order, "volume", tracked.volume) or 0)
        tracked.traded = float(getattr(order, "traded", tracked.traded) or 0)
        tracked.status = _enum_name(getattr(order, "status", tracked.status))
        tracked.updated_at = _now_text()
        if not _is_order_active(order, tracked.status):
            self._archive(vt_orderid)
        return tracked

    def on_stop_order(self, stop_order: Any) -> CtaTrackedOrder:
        vt_orderid = str(
            getattr(stop_order, "stop_orderid", None)
            or getattr(stop_order, "vt_orderid", "")
        )
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            tracked = CtaTrackedOrder(vt_orderid=vt_orderid, role="stop")
            self.active[vt_orderid] = tracked
        tracked.price = float(getattr(stop_order, "price", tracked.price) or 0)
        tracked.volume = float(getattr(stop_order, "volume", tracked.volume) or 0)
        tracked.status = _enum_name(getattr(stop_order, "status", tracked.status))
        tracked.updated_at = _now_text()
        if tracked.status in TERMINAL_STATUSES:
            self._archive(vt_orderid)
        return tracked

    def on_trade(self, trade: Any) -> CtaTrackedOrder | None:
        vt_orderid = str(getattr(trade, "vt_orderid", ""))
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            return None
        tracked.traded += float(getattr(trade, "volume", 0) or 0)
        tracked.updated_at = _now_text()
        if tracked.volume > 0 and tracked.traded >= tracked.volume:
            tracked.status = "ALLTRADED"
            self._archive(vt_orderid)
        elif tracked.traded > 0:
            tracked.status = "PARTTRADED"
        return tracked

    def to_json(self) -> str:
        payload = {
            "active": [asdict(order) for order in self.active.values()],
            "history": [asdict(order) for order in self.history[-20:]],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> CtaOrderStatusMachine:
        machine = cls()
        if not raw:
            return machine
        payload = json.loads(raw)
        for data in payload.get("active", []):
            order = CtaTrackedOrder(**data)
            if order.active:
                machine.active[order.vt_orderid] = order
            else:
                machine.history.append(order)
        for data in payload.get("history", []):
            machine.history.append(CtaTrackedOrder(**data))
        return machine

    def _archive(self, vt_orderid: str) -> None:
        tracked = self.active.pop(vt_orderid, None)
        if tracked is not None:
            tracked.updated_at = _now_text()
            self.history.append(tracked)


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _is_order_active(order: Any, status: str) -> bool:
    checker = getattr(order, "is_active", None)
    if callable(checker):
        return bool(checker())
    return status not in TERMINAL_STATUSES
