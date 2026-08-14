from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


TERMINAL_STATUSES = {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED", "TIMEDOUT"}
CANCELLING_STATUS = "CANCELLING"
EMPTY_ORDER_ID = ""
RISK_REJECTED_ROLE = "risk_rejected_or_submit_failed"
TERMINAL_PENDING_TRADES_STATUS = "TERMINAL_PENDING_TRADES"


class TradeDisposition(StrEnum):
    """Outcome of feeding one trade to the order state machine.

    The strategy updates PositionContext / risk / PnL ONLY on ACCEPTED.
    """
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"      # vt_tradeid already consumed
    LATE = "late"                # order already terminal (out-of-order report)
    UNKNOWN = "unknown"          # no tracked order for this vt_orderid
    INVALID_TRANSITION = "invalid_transition"


@dataclass(frozen=True, slots=True)
class TradeDispositionResult:
    disposition: TradeDisposition
    tracked: "CtaTrackedOrder | None" = None

    @property
    def accepted(self) -> bool:
        return self.disposition == TradeDisposition.ACCEPTED


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
    reported_traded_target: float = 0.0
    reported_status: str = ""

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL_STATUSES


class CtaOrderStatusMachine:
    """CTA order state machine with explicit trade dispositions.

    P7-R4 hardening:
      - on_trade() returns TradeDispositionResult (accepted/duplicate/late/
        unknown/invalid_transition); strategy updates business state only on
        ACCEPTED.
      - idempotency key is vt_tradeid (survives restart), not bare tradeid.
      - TIMEDOUT is a terminal status; timeout first requests a real cancel
        (CANCELLING + pending_cancel_orderids) and archives only on a
        terminal report.
      - single active close intent prevents duplicate close orders across bars.
    """

    TERMINAL_STATUSES = TERMINAL_STATUSES

    def __init__(self) -> None:
        self.active: dict[str, CtaTrackedOrder] = {}
        self.history: list[CtaTrackedOrder] = []
        self.consumed_trade_ids: set[str] = set()
        self.pending_cancel_orderids: list[str] = []
        self._close_intent: str | None = None

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

    # ── single active close intent ──

    def set_close_intent(self, vt_orderid: str) -> None:
        self._close_intent = vt_orderid

    def clear_close_intent(self) -> None:
        self._close_intent = None

    def has_active_close_intent(self) -> bool:
        return self._close_intent is not None

    @property
    def close_intent_orderid(self) -> str | None:
        return self._close_intent

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

    def on_order(
        self,
        order: Any,
        *,
        defer_reported_trades: bool = False,
    ) -> CtaTrackedOrder:
        vt_orderid = str(getattr(order, "vt_orderid", ""))
        if not vt_orderid:
            return self._record_empty_order_update()
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            tracked = CtaTrackedOrder(vt_orderid=vt_orderid, role="recovered")
            self.active[vt_orderid] = tracked
        processed_traded = tracked.traded
        reported_traded = float(getattr(order, "traded", tracked.traded) or 0)
        tracked.price = float(getattr(order, "price", tracked.price) or 0)
        tracked.volume = float(getattr(order, "volume", tracked.volume) or 0)
        reported_status = _enum_name(getattr(order, "status", tracked.status))
        tracked.traded = reported_traded
        tracked.status = reported_status
        tracked.updated_at = _now_text()
        if defer_reported_trades and reported_traded > processed_traded:
            tracked.traded = processed_traded
            tracked.reported_traded_target = reported_traded
            tracked.reported_status = reported_status
            if not _is_order_active(order, reported_status):
                tracked.status = TERMINAL_PENDING_TRADES_STATUS
            return tracked
        if not _is_order_active(order, reported_status):
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

    def on_trade(self, trade: Any) -> TradeDispositionResult:
        """Feed one fill; returns an explicit disposition.

        Idempotency key is vt_tradeid (survives restart).  The strategy only
        updates PositionContext / risk / PnL when disposition is ACCEPTED.
        """
        vt_tradeid = str(getattr(trade, "vt_tradeid", "") or getattr(trade, "tradeid", ""))
        if vt_tradeid and vt_tradeid in self.consumed_trade_ids:
            return TradeDispositionResult(TradeDisposition.DUPLICATE)

        vt_orderid = str(getattr(trade, "vt_orderid", ""))
        tracked = self.active.get(vt_orderid)
        if tracked is None:
            # Distinguish "was tracked but now terminal" (LATE) from a fill
            # that never belonged to any tracked order (UNKNOWN).
            if any(h.vt_orderid == vt_orderid for h in self.history):
                return TradeDispositionResult(TradeDisposition.LATE)
            return TradeDispositionResult(TradeDisposition.UNKNOWN)
        if tracked.status in TERMINAL_STATUSES:
            return TradeDispositionResult(TradeDisposition.LATE)

        volume = float(getattr(trade, "volume", 0) or 0)
        if volume <= 0:
            return TradeDispositionResult(TradeDisposition.INVALID_TRANSITION, tracked)

        tracked.traded += volume
        tracked.updated_at = _now_text()
        if vt_tradeid:
            tracked.trade_ids.append(vt_tradeid)
            self.consumed_trade_ids.add(vt_tradeid)
        if (
            tracked.reported_traded_target > 0
            and tracked.traded >= tracked.reported_traded_target
        ):
            reported_status = tracked.reported_status
            tracked.reported_traded_target = 0.0
            tracked.reported_status = ""
            if reported_status in TERMINAL_STATUSES:
                tracked.status = reported_status
                self._archive(vt_orderid)
                return TradeDispositionResult(TradeDisposition.ACCEPTED, tracked)
            tracked.status = reported_status
        if tracked.volume > 0 and tracked.traded >= tracked.volume:
            tracked.status = "ALLTRADED"
            self._archive(vt_orderid)
        elif tracked.traded > 0:
            tracked.status = "PARTTRADED"
        return TradeDispositionResult(TradeDisposition.ACCEPTED, tracked)

    def check_timeouts(self, now: str | None = None) -> list[str]:
        """Mark timed-out orders CANCELLING and request a real cancel.

        Does NOT archive: the order archives only after the exchange reports
        a terminal status.  Returns order ids that entered CANCELLING.
        """
        now_text = now or _now_text()
        now_ts = datetime.fromisoformat(now_text)
        cancelling: list[str] = []
        for order_id, tracked in list(self.active.items()):
            if not tracked.timeout_at:
                continue
            try:
                deadline = datetime.fromisoformat(tracked.timeout_at)
            except ValueError:
                continue
            if now_ts >= deadline and tracked.status != CANCELLING_STATUS:
                tracked.status = CANCELLING_STATUS
                tracked.updated_at = now_text
                if order_id not in self.pending_cancel_orderids:
                    self.pending_cancel_orderids.append(order_id)
                cancelling.append(order_id)
        return cancelling

    def to_json(self) -> str:
        payload = {
            "active": [asdict(order) for order in self.active.values()],
            "history": [asdict(order) for order in self.history[-30:]],
            "consumed_trade_ids": sorted(self.consumed_trade_ids),
            "pending_cancel_orderids": list(self.pending_cancel_orderids),
            "close_intent": self._close_intent,
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
        machine.pending_cancel_orderids = list(payload.get("pending_cancel_orderids") or [])
        machine._close_intent = payload.get("close_intent")
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
