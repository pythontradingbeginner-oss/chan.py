from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from Common.CEnum import BSP_TYPE


@dataclass(frozen=True)
class StrategySignal:
    timestamp: object
    action: str
    target_position: int
    price: float
    reason: str
    bsp_type: str
    bsp_bi_idx: int
    bsp_klu_idx: int
    active_symbol: str | None = None


class MinimalChanTrendStrategy:
    """Trade with the newest chan.py buy/sell point as the regime trigger."""

    def __init__(
        self,
        *,
        accepted_bsp_types: Iterable[BSP_TYPE] | None = None,
        allow_short: bool = True,
        require_confirmed_bsp: bool = True,
    ) -> None:
        self.accepted_bsp_types = set(
            accepted_bsp_types
            if accepted_bsp_types is not None
            else (BSP_TYPE.T1, BSP_TYPE.T1P, BSP_TYPE.T2, BSP_TYPE.T3A, BSP_TYPE.T3B)
        )
        self.allow_short = allow_short
        self.require_confirmed_bsp = require_confirmed_bsp
        self._consumed_keys: set[tuple[int, int, str, bool]] = set()

    def on_bar(
        self,
        *,
        chan,
        current_position: int,
        price: float,
        timestamp: object,
        active_symbol: str | None = None,
        lv_idx: int = 0,
    ) -> StrategySignal | None:
        latest = chan.get_latest_bsp(idx=lv_idx, number=1)
        if not latest:
            return None
        bsp = latest[0]
        if not any(bsp_type in self.accepted_bsp_types for bsp_type in bsp.type):
            return None
        if self.require_confirmed_bsp and not _bsp_is_on_last_confirmed_klc(chan[lv_idx], bsp):
            return None

        key = (bsp.bi.idx, bsp.klu.idx, bsp.type2str(), bsp.is_buy)
        if key in self._consumed_keys:
            return None

        target_position = self._target_position(bsp.is_buy, current_position)
        self._consumed_keys.add(key)
        if target_position == current_position:
            return None

        return StrategySignal(
            timestamp=timestamp,
            action=_action_name(current_position, target_position),
            target_position=target_position,
            price=float(price),
            reason="chan_bsp",
            bsp_type=bsp.type2str(),
            bsp_bi_idx=bsp.bi.idx,
            bsp_klu_idx=bsp.klu.idx,
            active_symbol=active_symbol,
        )

    def _target_position(self, is_buy: bool, current_position: int) -> int:
        if is_buy:
            return 1
        if self.allow_short:
            return -1
        return 0 if current_position > 0 else current_position


def _bsp_is_on_last_confirmed_klc(kl_list, bsp) -> bool:
    if len(kl_list) < 2:
        return False
    return bsp.klu.klc.idx == kl_list[-2].idx


def _action_name(current_position: int, target_position: int) -> str:
    if current_position == 0 and target_position > 0:
        return "open_long"
    if current_position == 0 and target_position < 0:
        return "open_short"
    if current_position > 0 and target_position == 0:
        return "close_long"
    if current_position < 0 and target_position == 0:
        return "close_short"
    if current_position > 0 and target_position < 0:
        return "reverse_long_to_short"
    if current_position < 0 and target_position > 0:
        return "reverse_short_to_long"
    return "hold"
