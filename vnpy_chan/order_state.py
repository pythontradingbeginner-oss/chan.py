from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


TERMINAL_STATUSES = {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED"}
CANCELLING_STATUS = "CANCELLING"
EMPTY_ORDER_ID = ""
RISK_REJECTED_ROLE = "risk_rejected_or_submit_failed"


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
    timeout_at: str = ""
    trade_ids: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL_STATUSES


class CtaOrderStatusMachine:
    """CTA order state machine with fill dedup, cancellation and timeout.

    P7.3 hardening:
      - duplicate fills are detected and dropped via consumed trade ids
      - a terminal order never accepts a later trade (out-of-order report)
      - CANCELLING is tracked as a live (non-terminal) status
      - empty order ids record risk_rejected_or_submit_failed
    """

    def __init__(self) -> None:
        self.active: dict[str, CtaTrackedOrder] = {}
        self.history: list[CtaTrackedOrder] = []
        self.consumed_trade_ids: set[str] = set()

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
        timeout_at: str = "",
    ) -> None:
        for vt_orderid in vt_orderids:
            order_id = str(vt_orderid)
            if not order_id:
                self._record_empty_submit(role, price, volume, reason)
                continue
            self.active[order_id] = CtaTrackedOrder(
                vt_orderid=order_id,
                role=role,
                price=float(price),
                volume=float(volume),
                reason=reason,
                timeout_at=timeout_at,
            )

    def _record_empty_submit(
        self,
        role: str,
        price: float,
        volume: float,
        reason: str,
    ) -> None:
        entry = CtaTrackedOrder(
            vt_orderid="",
            role=RISK_REJECTED_ROLE,
            price=float(price),
            volume=float(volume),
            status="REJECTED",
            reason=f"empty_vt_orderid:role={role}:{reason}",
        )
        self.history.append(entry)

    def on_order(self, order: Any) -> CtaTrackedOrder:
        vt_orderid = str(getattr(order, "vt_orderid", ""))
        if not vt_orderid:
            return self._record_empty_order_update()
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

    def _record_empty_order_update(self) -> CtaTrackedOrder:
        entry = CtaTrackedOrder(
            vt_orderid="",
            role=RISK_REJECTED_ROLE,
            status="REJECTED",
            reason="empty_vt_orderid_order_update",
        )
        self.history.append(entry)
        return entry

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
        trade_id = str(getattr(trade, "tradeid", ""))
        if trade_id in self.consumed_trade_ids:
            return None
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            return None
        if tracked.status in TERMINAL_STATUSES:
            # Out-of-order report: a terminal order cannot fill again.
            return tracked
        volume = float(getattr(trade, "volume", 0) or 0)
        if volume <= 0:
            return tracked
        tracked.traded += volume
        tracked.updated_at = _now_text()
        if trade_id:
            tracked.trade_ids.append(trade_id)
            self.consumed_trade_ids.add(trade_id)
        if tracked.volume > 0 and tracked.traded >= tracked.volume:
            tracked.status = "ALLTRADED"
            self._archive(vt_orderid)
        elif tracked.traded > 0:
            tracked.status = "PARTTRADED"
        return tracked

    def check_timeouts(self, now: str | None = None) -> list[str]:
        """Return ids whose timeout_at has passed and archive them as timed out."""
        now_text = now or _now_text()
        now_ts = datetime.fromisoformat(now_text)
        timed_out: list[str] = []
        for order_id, tracked in list(self.active.items()):
            if not tracked.timeout_at:
                continue
            try:
                deadline = datetime.fromisoformat(tracked.timeout_at)
            except ValueError:
                continue
            if now_ts >= deadline:
                tracked.status = "TIMEDOUT"
                tracked.updated_at = now_text
                timed_out.append(order_id)
                self._archive(order_id)
        return timed_out

    def to_json(self) -> str:
        payload = {
            "active": [asdict(order) for order in self.active.values()],
            "history": [asdict(order) for order in self.history[-30:]],
            "consumed_trade_ids": sorted(self.consumed_trade_ids),
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
        machine.consumed_trade_ids = set(payload.get("consumed_trade_ids") or [])
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
