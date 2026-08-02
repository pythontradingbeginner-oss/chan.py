"""Point-in-time parent direction compatibility filter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from Common.CEnum import KL_TYPE

from .multi_level import (
    ParentDirection,
    ParentStructureSnapshot,
    klu_timestamp,
    market_timestamp,
)


class ParentTrendAction(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    DOWNGRADE = "downgrade"


@dataclass(frozen=True)
class ParentTrendResult:
    action: ParentTrendAction
    parent_direction: str
    parent_seg_idx: int
    detail: str
    parent_available_at: object | None = None
    decision_time: object | None = None
    time_honest: bool = False
    reason_codes: tuple[str, ...] = ()


class ParentTrendFilter:
    """Filter an entry using only parent structure visible at decision time."""

    def __init__(
        self,
        *,
        parent_lv: KL_TYPE | None = None,
        require_confirmed_seg: bool = False,
        downgrade_on_unknown: bool = True,
    ) -> None:
        self._parent_lv = parent_lv
        self._require_confirmed = require_confirmed_seg
        self._downgrade_on_unknown = downgrade_on_unknown

    def filter(
        self,
        chan,
        signal_direction: str,
        *,
        lv_idx: int = 0,
        decision_time: object | None = None,
        parent_snapshot: ParentStructureSnapshot | None = None,
    ) -> ParentTrendResult:
        if decision_time is None:
            return self._unknown(
                "缺少 decision_time，无法证明父级信息在当时可见",
                reason="decision_time_required",
            )
        cutoff = market_timestamp(decision_time)
        snapshot = parent_snapshot or self._snapshot_from_chan(
            chan, cutoff=cutoff, lv_idx=lv_idx
        )
        if snapshot is None:
            return self._unknown(
                "父级别已确认结构不可用",
                cutoff=cutoff,
                reason="parent_structure_unavailable",
            )

        available_at = market_timestamp(snapshot.available_at)
        time_honest = (
            available_at <= cutoff
            and market_timestamp(snapshot.structure_end_time) <= available_at
        )
        if not time_honest:
            return ParentTrendResult(
                action=ParentTrendAction.REJECT,
                parent_direction=snapshot.direction.value,
                parent_seg_idx=snapshot.structure_idx,
                detail="父级结构在当前决策时刻尚不可见",
                parent_available_at=available_at,
                decision_time=cutoff,
                time_honest=False,
                reason_codes=("parent_not_available_at_decision",),
            )
        if self._require_confirmed and not snapshot.is_confirmed:
            return self._unknown(
                "父级结构尚未确认",
                cutoff=cutoff,
                reason="parent_structure_unconfirmed",
            )

        aligned = (
            signal_direction == "long"
            and snapshot.direction == ParentDirection.BULLISH
        ) or (
            signal_direction == "short"
            and snapshot.direction == ParentDirection.BEARISH
        )
        action = ParentTrendAction.PASS if aligned else ParentTrendAction.REJECT
        direction = "up" if snapshot.direction == ParentDirection.BULLISH else "down"
        return ParentTrendResult(
            action=action,
            parent_direction=direction,
            parent_seg_idx=snapshot.structure_idx,
            detail=(
                f"父级别方向={direction}, 信号方向={signal_direction} -> "
                f"{action.value}"
            ),
            parent_available_at=available_at,
            decision_time=cutoff,
            time_honest=True,
            reason_codes=() if aligned else ("parent_direction_conflict",),
        )

    def _snapshot_from_chan(
        self,
        chan,
        *,
        cutoff: object,
        lv_idx: int,
    ) -> ParentStructureSnapshot | None:
        try:
            if self._parent_lv is not None:
                parent_data = chan[self._parent_lv]
                level = self._parent_lv.value
            else:
                if lv_idx <= 0 or lv_idx >= len(chan.lv_list):
                    return None
                parent_lv = chan.lv_list[lv_idx - 1]
                parent_data = chan[parent_lv]
                level = parent_lv.value
        except (AttributeError, IndexError, KeyError, TypeError):
            return None

        selected = None
        for segment in reversed(list(parent_data.seg_list)):
            if not self._require_confirmed or bool(segment.is_sure):
                selected = segment
                break
        if selected is None:
            return None
        direction = (
            ParentDirection.BULLISH if selected.is_up() else ParentDirection.BEARISH
        )
        return ParentStructureSnapshot(
            snapshot_id=f"snapshot:{level}:seg:{selected.idx}",
            level=str(level),
            direction=direction,
            structure_kind="segment",
            structure_idx=int(selected.idx),
            structure_begin_time=klu_timestamp(selected.get_begin_klu()),
            structure_end_time=klu_timestamp(selected.get_end_klu()),
            available_at=cutoff,
            is_confirmed=bool(selected.is_sure),
        )

    def _unknown(
        self,
        detail: str,
        *,
        cutoff: object | None = None,
        reason: str,
    ) -> ParentTrendResult:
        return ParentTrendResult(
            action=(
                ParentTrendAction.DOWNGRADE
                if self._downgrade_on_unknown
                else ParentTrendAction.PASS
            ),
            parent_direction="unknown",
            parent_seg_idx=-1,
            detail=detail,
            decision_time=cutoff,
            time_honest=cutoff is not None,
            reason_codes=(reason,),
        )
