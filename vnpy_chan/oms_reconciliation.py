"""OMS reconciliation for P7.2 restart recovery.

Compares the CTA strategy's in-memory state (PositionContext + order machine)
against the actual OMS positions and active orders reported by the gateway.

Outcomes are a strict lattice:
  ALIGNED                    -> everything matches, normal operation
  POSITION_MISMATCH          -> strategy position differs from OMS
  ACTIVE_ORDER_MISMATCH      -> active orders differ from OMS
  UNATTRIBUTED_POSITION      -> OMS holds a position the strategy never opened
  RECOVERY_REQUIRED          -> any mismatch above; open blocked, close allowed

The strategy must never auto-claim a position it cannot attribute to one of its
own fills.  An UNATTRIBUTED_POSITION therefore forces RECOVERY_REQUIRED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategy_policy.position import PositionContext


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: str
    oms_net_position: int
    strategy_net_position: int
    oms_active_orders: tuple[str, ...] = ()
    strategy_active_orders: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def aligned(self) -> bool:
        return self.status == "ALIGNED"

    @property
    def recovery_required(self) -> bool:
        return self.status == "RECOVERY_REQUIRED"

    @property
    def allow_open(self) -> bool:
        return self.aligned

    @property
    def allow_close(self) -> bool:
        # Closing is always safe: it reduces an OMS position the broker owns.
        return True

    @property
    def reason_text(self) -> str:
        return ";".join(self.reasons)


def oms_net_position(positions: list[Any], symbol: str) -> int:
    """Signed net position for ``symbol`` from a list of OMS PositionData."""
    net = 0
    symbol = symbol.lower()
    for position in positions:
        if str(getattr(position, "symbol", "")).lower() != symbol:
            continue
        volume = int(getattr(position, "volume", 0) or 0)
        direction = str(getattr(position, "direction", ""))
        if "LONG" in direction.upper():
            net += volume
        elif "SHORT" in direction.upper():
            net -= volume
    return net


def oms_active_order_ids(orders: list[Any]) -> tuple[str, ...]:
    """Order ids still active (not terminal) reported by OMS."""
    terminal = {"ALLTRADED", "CANCELLED", "REJECTED", "TRIGGERED"}
    active: list[str] = []
    for order in orders:
        status = str(getattr(order, "status", "")).upper()
        if status in terminal:
            continue
        orderid = str(getattr(order, "vt_orderid", "") or getattr(order, "orderid", ""))
        if orderid:
            active.append(orderid)
    return tuple(sorted(active))


def signed_volume(context: PositionContext | None) -> int:
    """Strategy-side signed position (long positive, short negative)."""
    if context is None:
        return 0
    from signal_core.models import SignalDirection

    sign = 1 if context.direction == SignalDirection.LONG else -1
    return sign * context.volume


def reconcile(
    *,
    oms_positions: list[Any],
    oms_orders: list[Any],
    symbol: str,
    strategy_context: PositionContext | None,
    strategy_active_order_ids: tuple[str, ...] = (),
) -> ReconciliationResult:
    """Build the reconciliation outcome for one restart."""
    oms_pos = oms_net_position(oms_positions, symbol)
    strategy_pos = signed_volume(strategy_context)
    oms_active = oms_active_order_ids(oms_orders)
    strategy_active = tuple(sorted(strategy_active_order_ids))
    reasons: list[str] = []

    if oms_pos != strategy_pos:
        reasons.append(
            f"position_mismatch:oms={oms_pos} strategy={strategy_pos}"
        )

    if set(oms_active) != set(strategy_active):
        reasons.append(
            "active_order_mismatch:"
            f"oms={','.join(oms_active) or 'none'} "
            f"strategy={','.join(strategy_active) or 'none'}"
        )

    if oms_pos != 0 and strategy_context is None:
        reasons.append(
            f"unattributed_position:oms_net={oms_pos} has_no_strategy_context"
        )

    if reasons:
        return ReconciliationResult(
            status="RECOVERY_REQUIRED",
            oms_net_position=oms_pos,
            strategy_net_position=strategy_pos,
            oms_active_orders=oms_active,
            strategy_active_orders=strategy_active,
            reasons=tuple(reasons),
        )

    return ReconciliationResult(
        status="ALIGNED",
        oms_net_position=oms_pos,
        strategy_net_position=strategy_pos,
        oms_active_orders=oms_active,
        strategy_active_orders=strategy_active,
    )
