"""Timestamp-based interval nesting confirmation.

K-line and stroke indices are local to a level and must never be compared
across levels.  This compatibility API now joins by structural time and, when
provided, the child signal's first-visible time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .multi_level import SubLevelSignalSnapshot, klu_timestamp, market_timestamp


@dataclass(frozen=True)
class IntervalResult:
    confirmed: bool
    sub_bsp_count: int
    sub_bsp_types: str
    detail: str
    window_begin: object | None = None
    window_end: object | None = None
    decision_time: object | None = None
    matched_signal_times: tuple[object, ...] = ()
    matched_available_at: tuple[object, ...] = ()
    time_honest: bool = False
    reason_codes: tuple[str, ...] = ()


class IntervalConfirmer:
    """Confirm a current-level BSP with visible, same-direction child BSPs."""

    def __init__(
        self,
        *,
        require_sure_bi: bool = True,
        sub_lv_bsp_types: frozenset[str] = frozenset({"1", "1p", "2"}),
    ) -> None:
        self._require_sure_bi = require_sure_bi
        self._sub_bsp_types = sub_lv_bsp_types

    def confirm(
        self,
        chan,
        bsp_bi_idx: int,
        bsp_direction: str,
        *,
        lv_idx: int = 0,
        decision_time: object | None = None,
        child_signals: Iterable[SubLevelSignalSnapshot] | None = None,
    ) -> IntervalResult:
        if decision_time is None:
            return IntervalResult(
                False,
                0,
                "",
                "缺少 decision_time，无法证明次级别信息在当时可见",
                reason_codes=("decision_time_required",),
            )
        cutoff = market_timestamp(decision_time)

        try:
            current_data = chan[lv_idx]
            if bsp_bi_idx < 0 or bsp_bi_idx >= len(current_data.bi_list):
                raise IndexError
            target_bi = current_data.bi_list[bsp_bi_idx]
        except (KeyError, IndexError, AttributeError, TypeError):
            return IntervalResult(
                False,
                0,
                "",
                f"bi_idx={bsp_bi_idx} 不可用",
                decision_time=cutoff,
                reason_codes=("current_bi_unavailable",),
            )
        if self._require_sure_bi and not target_bi.is_sure:
            return IntervalResult(
                False,
                0,
                "",
                "本级别笔未确认",
                decision_time=cutoff,
                reason_codes=("current_bi_unconfirmed",),
            )

        begin = klu_timestamp(target_bi.get_begin_klu())
        end = klu_timestamp(target_bi.get_end_klu())
        if child_signals is not None:
            matches = [
                signal
                for signal in child_signals
                if signal.direction == bsp_direction
                and signal.is_confirmed
                and bool(self._sub_bsp_types.intersection(signal.bsp_types))
                and begin <= market_timestamp(signal.signal_time) <= end
                and market_timestamp(signal.available_at) <= cutoff
            ]
            return self._result(matches, begin=begin, end=end, cutoff=cutoff)

        try:
            if lv_idx + 1 >= len(chan.lv_list):
                raise IndexError
            sub_data = chan[lv_idx + 1]
            bsp_list = sub_data.bs_point_lst
        except (KeyError, IndexError, AttributeError, TypeError):
            return IntervalResult(
                False,
                0,
                "",
                "次级别数据不可用",
                window_begin=begin,
                window_end=end,
                decision_time=cutoff,
                time_honest=True,
                reason_codes=("sublevel_data_unavailable",),
            )

        matches: list[SubLevelSignalSnapshot] = []
        for bsp in bsp_list.bsp_iter():
            direction = "long" if bool(bsp.is_buy) else "short"
            types = tuple(item.value for item in bsp.type)
            signal_time = klu_timestamp(bsp.klu)
            if direction != bsp_direction or not begin <= signal_time <= end:
                continue
            if self._require_sure_bi and not bsp.bi.is_sure:
                continue
            if not self._sub_bsp_types.intersection(types):
                continue
            # The caller's CChan is a point-in-time snapshot.  Therefore the
            # observation time of a BSP present in it is the decision time.
            matches.append(
                SubLevelSignalSnapshot(
                    signal_id=f"snapshot:{lv_idx + 1}:{bsp.bi.idx}:{direction}",
                    level=str(chan.lv_list[lv_idx + 1]),
                    bi_idx=int(bsp.bi.idx),
                    direction=direction,
                    bsp_types=types,
                    signal_time=signal_time,
                    bi_begin_time=klu_timestamp(bsp.bi.get_begin_klu()),
                    bi_end_time=klu_timestamp(bsp.bi.get_end_klu()),
                    available_at=cutoff,
                    is_confirmed=bool(bsp.bi.is_sure),
                )
            )
        return self._result(matches, begin=begin, end=end, cutoff=cutoff)

    @staticmethod
    def _result(
        matches: list[SubLevelSignalSnapshot],
        *,
        begin: object,
        end: object,
        cutoff: object,
    ) -> IntervalResult:
        types = tuple(sorted({value for match in matches for value in match.bsp_types}))
        confirmed = bool(matches)
        return IntervalResult(
            confirmed=confirmed,
            sub_bsp_count=len(matches),
            sub_bsp_types=",".join(types),
            detail=(
                f"区间套确认: 次级别 {len(matches)} 个同向 BSP ({','.join(types)})"
                if confirmed
                else "当前决策时刻无可见的次级别同向 BSP"
            ),
            window_begin=begin,
            window_end=end,
            decision_time=cutoff,
            matched_signal_times=tuple(match.signal_time for match in matches),
            matched_available_at=tuple(match.available_at for match in matches),
            time_honest=all(
                market_timestamp(match.available_at) <= market_timestamp(cutoff)
                for match in matches
            ),
            reason_codes=() if confirmed else ("sublevel_confirmation_missing",),
        )
