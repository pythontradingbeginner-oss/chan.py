"""P7.4 hard risk interception layer.

Independent of the VeighNa ConnectionMonitorStrategy.  Every rule below blocks
ONLY opening orders; closing/risk-reducing orders always pass.  A single open
is refused if ANY rule fires.

Rules:
  RB_CONTRACT_WHITELIST   vt_symbol must be a supported RB main contract.
  SINGLE_LOT_LIMIT        planned open size must not exceed one lot.
  MANUAL_HALT             an explicit operator halt flag refuses opens.
  MINIMUM_EQUITY          account equity below a floor refuses opens.
  MARKET_HEARTBEAT        no fresh tick for N seconds refuses opens.
  ACCOUNT_HEARTBEAT       no fresh account update for N seconds refuses opens.
  DAILY_LOSS_LIMIT        realized day loss beyond the configured limit.
  MAX_DRAWDOWN            equity drawdown beyond the configured pct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


SUPPORTED_RB_MAIN_PREFIXES = ("rb", "RB")


@dataclass(frozen=True, slots=True)
class OpenDecision:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason_text(self) -> str:
        return ";".join(self.reasons)


@dataclass(slots=True)
class HardRiskState:
    """Mutable inputs refreshed by the strategy before each open decision."""

    vt_symbol: str = ""
    planned_open_lots: int = 1
    manual_halt: bool = False
    minimum_equity: float = 0.0
    account_equity: float | None = None
    last_tick_time: datetime | None = None
    last_account_time: datetime | None = None
    tick_timeout_seconds: float = 120.0
    account_timeout_seconds: float = 30.0
    realized_day_loss: float | None = None
    daily_loss_limit: float | None = None
    drawdown_pct: float | None = None
    max_drawdown_pct: float | None = None
    active_main_contracts: tuple[str, ...] = ()


def evaluate_open_guard(state: HardRiskState) -> OpenDecision:
    """Evaluate all hard rules; a single failure refuses the open."""
    reasons: list[str] = []

    symbol = str(state.vt_symbol)
    base = symbol.split(".")[0]
    if not base.upper().startswith("RB") or not any(
        c.isdigit() for c in base
    ):
        reasons.append(f"rb_contract_whitelist:{symbol}")

    if state.active_main_contracts and base.upper() not in {
        str(value).split(".")[0].upper() for value in state.active_main_contracts
    }:
        reasons.append(
            f"contract_rollover_required:{base} not in "
            f"{','.join(state.active_main_contracts)}"
        )

    if state.planned_open_lots > 1:
        reasons.append(f"single_lot_limit:{state.planned_open_lots}>1")

    if state.manual_halt:
        reasons.append("manual_halt")

    if state.minimum_equity > 0 and state.account_equity is not None:
        if state.account_equity < state.minimum_equity:
            reasons.append(
                f"minimum_equity:{state.account_equity:.0f}<{state.minimum_equity:.0f}"
            )

    now = datetime.now()
    if state.last_tick_time is not None and state.tick_timeout_seconds > 0:
        if now - state.last_tick_time > timedelta(seconds=state.tick_timeout_seconds):
            reasons.append("market_heartbeat_timeout")
    elif state.last_tick_time is None and state.account_equity is not None:
        # No tick yet but equity present: market feed not yet verified live.
        reasons.append("market_heartbeat_unverified")

    if state.last_account_time is not None and state.account_timeout_seconds > 0:
        if now - state.last_account_time > timedelta(
            seconds=state.account_timeout_seconds
        ):
            reasons.append("account_heartbeat_timeout")
    elif state.last_account_time is None and state.account_equity is not None:
        reasons.append("account_heartbeat_unverified")

    if state.daily_loss_limit is not None and state.realized_day_loss is not None:
        if state.realized_day_loss <= -abs(state.daily_loss_limit):
            reasons.append(
                f"daily_loss_limit:{state.realized_day_loss:.0f}<=-{abs(state.daily_loss_limit):.0f}"
            )

    if state.max_drawdown_pct is not None and state.drawdown_pct is not None:
        if state.drawdown_pct >= state.max_drawdown_pct:
            reasons.append(
                f"max_drawdown:{state.drawdown_pct:.2%}>={state.max_drawdown_pct:.2%}"
            )

    return OpenDecision(allowed=not reasons, reasons=tuple(reasons))
