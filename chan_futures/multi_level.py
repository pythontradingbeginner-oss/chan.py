"""Point-in-time multi-level context for Qingpai entry decisions.

Each level advances independently.  Cross-level joins use market timestamps and
the first time a structure became observable, never the level-local K-line idx.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE

from .feed import prepare_ohlc_frame, row_to_klu


_KL_TYPE_MAP: dict[str, KL_TYPE] = {
    "K_1M": KL_TYPE.K_1M,
    "K_5M": KL_TYPE.K_5M,
    "K_15M": KL_TYPE.K_15M,
    "K_30M": KL_TYPE.K_30M,
    "K_60M": KL_TYPE.K_60M,
    "K_DAY": KL_TYPE.K_DAY,
    "K_WEEK": KL_TYPE.K_WEEK,
    "K_MON": KL_TYPE.K_MON,
}


class ParentDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True, slots=True)
class MultiLevelReadiness:
    """Read-only structural readiness for the live CTA adapter."""

    parent_confirmed_segments: int
    child_confirmed_bis: int

    @property
    def parent_ready(self) -> bool:
        return self.parent_confirmed_segments > 0

    @property
    def child_ready(self) -> bool:
        return self.child_confirmed_bis > 0


@dataclass(frozen=True, slots=True)
class ParentStructureSnapshot:
    """A parent structure and the instant it first became tradable knowledge."""

    snapshot_id: str
    level: str
    direction: ParentDirection
    structure_kind: str
    structure_idx: int
    structure_begin_time: object
    structure_end_time: object
    available_at: object
    is_confirmed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "level": self.level,
            "direction": self.direction.value,
            "structure_kind": self.structure_kind,
            "structure_idx": self.structure_idx,
            "structure_begin_time": self.structure_begin_time,
            "structure_end_time": self.structure_end_time,
            "available_at": self.available_at,
            "is_confirmed": self.is_confirmed,
        }


@dataclass(frozen=True, slots=True)
class SubLevelSignalSnapshot:
    """A child-level BSP with separate structural and availability times."""

    signal_id: str
    level: str
    bi_idx: int
    direction: str
    bsp_types: tuple[str, ...]
    signal_time: object
    bi_begin_time: object
    bi_end_time: object
    available_at: object
    is_confirmed: bool
    revision: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "level": self.level,
            "bi_idx": self.bi_idx,
            "direction": self.direction,
            "bsp_types": self.bsp_types,
            "signal_time": self.signal_time,
            "bi_begin_time": self.bi_begin_time,
            "bi_end_time": self.bi_end_time,
            "available_at": self.available_at,
            "is_confirmed": self.is_confirmed,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class MultiLevelDecisionContext:
    """Auditable parent/child evidence used for one entry decision."""

    decision_time: object
    accepted: bool
    time_honest: bool
    reason_codes: tuple[str, ...]
    parent_required: bool
    parent_snapshot: ParentStructureSnapshot | None
    parent_action: str
    child_required: bool
    child_window_begin: object | None
    child_window_end: object | None
    child_confirmed: bool
    child_matches: tuple[SubLevelSignalSnapshot, ...]
    parent_last_observed_at: object | None = None
    child_last_observed_at: object | None = None

    def to_dict(self) -> dict[str, object]:
        parent = self.parent_snapshot
        return {
            "decision_time": self.decision_time,
            "accepted": self.accepted,
            "time_honest": self.time_honest,
            "reason_codes": self.reason_codes,
            "parent_required": self.parent_required,
            "parent_snapshot_id": parent.snapshot_id if parent else "",
            "parent_direction": parent.direction.value if parent else "",
            "parent_structure_kind": parent.structure_kind if parent else "",
            "parent_structure_idx": parent.structure_idx if parent else None,
            "parent_available_at": parent.available_at if parent else None,
            "parent_action": self.parent_action,
            "parent_last_observed_at": self.parent_last_observed_at,
            "child_required": self.child_required,
            "child_window_begin": self.child_window_begin,
            "child_window_end": self.child_window_end,
            "child_confirmed": self.child_confirmed,
            "child_match_count": len(self.child_matches),
            "child_match_ids": tuple(match.signal_id for match in self.child_matches),
            "child_signal_times": tuple(
                match.signal_time for match in self.child_matches
            ),
            "child_bsp_types": tuple(
                sorted({value for match in self.child_matches for value in match.bsp_types})
            ),
            "child_available_at": tuple(match.available_at for match in self.child_matches),
            "child_last_observed_at": self.child_last_observed_at,
        }


class ParentStructureTracker:
    """Append-only point-in-time history of parent directions."""

    def __init__(self, *, level: str, require_confirmed: bool = True) -> None:
        self.level = level
        self.require_confirmed = require_confirmed
        self._history: list[ParentStructureSnapshot] = []
        self._observation_times: list[pd.Timestamp] = []
        self.last_observed_at: pd.Timestamp | None = None

    def observe(self, chan: CChan, *, observed_at: object, lv_idx: int = 0) -> None:
        available_at = market_timestamp(observed_at)
        self._observation_times.append(available_at)
        self.last_observed_at = available_at
        level_data = chan[lv_idx]
        segments = list(level_data.seg_list)
        selected = None
        for segment in reversed(segments):
            if not self.require_confirmed or bool(segment.is_sure):
                selected = segment
                break
        if selected is None:
            return

        direction = (
            ParentDirection.BULLISH if selected.is_up() else ParentDirection.BEARISH
        )
        begin_time = klu_timestamp(selected.get_begin_klu())
        end_time = klu_timestamp(selected.get_end_klu())
        payload = {
            "level": self.level,
            "kind": "segment",
            "idx": int(selected.idx),
            "direction": direction.value,
            "begin": begin_time.isoformat(),
            "end": end_time.isoformat(),
            "confirmed": bool(selected.is_sure),
        }
        snapshot = ParentStructureSnapshot(
            snapshot_id=_stable_id("parent", payload),
            level=self.level,
            direction=direction,
            structure_kind="segment",
            structure_idx=int(selected.idx),
            structure_begin_time=begin_time,
            structure_end_time=end_time,
            available_at=available_at,
            is_confirmed=bool(selected.is_sure),
        )
        self.record(snapshot)

    def record(self, snapshot: ParentStructureSnapshot) -> None:
        normalized = replace(
            snapshot,
            structure_begin_time=market_timestamp(snapshot.structure_begin_time),
            structure_end_time=market_timestamp(snapshot.structure_end_time),
            available_at=market_timestamp(snapshot.available_at),
        )
        if self._history and self._same_semantics(self._history[-1], normalized):
            return
        self._history.append(normalized)
        self._history.sort(key=lambda item: market_timestamp(item.available_at))
        self._observation_times.append(market_timestamp(normalized.available_at))
        self.last_observed_at = max(
            filter(
                lambda value: value is not None,
                (self.last_observed_at, market_timestamp(normalized.available_at)),
            )
        )

    def as_of(self, decision_time: object) -> ParentStructureSnapshot | None:
        cutoff = market_timestamp(decision_time)
        for snapshot in reversed(self._history):
            if market_timestamp(snapshot.available_at) <= cutoff:
                return snapshot
        return None

    def last_observed_as_of(self, decision_time: object) -> pd.Timestamp | None:
        cutoff = market_timestamp(decision_time)
        values = [value for value in self._observation_times if value <= cutoff]
        return max(values) if values else None

    @property
    def history(self) -> tuple[ParentStructureSnapshot, ...]:
        return tuple(self._history)

    @staticmethod
    def _same_semantics(
        left: ParentStructureSnapshot,
        right: ParentStructureSnapshot,
    ) -> bool:
        return (
            left.snapshot_id == right.snapshot_id
            and left.is_confirmed == right.is_confirmed
        )


class SubLevelSignalTracker:
    """Append-only child BSP revisions captured when each revision is visible."""

    def __init__(self, *, level: str) -> None:
        self.level = level
        self._history: list[SubLevelSignalSnapshot] = []
        self._observation_times: list[pd.Timestamp] = []
        self._latest_fingerprint: dict[tuple[int, str], tuple[object, ...]] = {}
        self._revisions: dict[tuple[int, str], int] = {}
        self.last_observed_at: pd.Timestamp | None = None

    def observe(self, chan: CChan, *, observed_at: object, lv_idx: int = 0) -> None:
        available_at = market_timestamp(observed_at)
        self._observation_times.append(available_at)
        self.last_observed_at = available_at
        bsp_list = chan[lv_idx].bs_point_lst
        if bsp_list is None:
            return
        stable_confirmed = 0
        for scan_count, bsp in enumerate(bsp_list.bsp_iter_v2()):
            direction = "long" if bool(bsp.is_buy) else "short"
            key = (int(bsp.bi.idx), direction)
            bsp_types = tuple(sorted({item.value for item in bsp.type}))
            signal_time = klu_timestamp(bsp.klu)
            begin_time = klu_timestamp(bsp.bi.get_begin_klu())
            end_time = klu_timestamp(bsp.bi.get_end_klu())
            confirmed = bool(bsp.bi.is_sure)
            fingerprint = (
                bsp_types,
                signal_time,
                begin_time,
                end_time,
                confirmed,
            )
            if self._latest_fingerprint.get(key) == fingerprint:
                if confirmed:
                    stable_confirmed += 1
                if stable_confirmed >= 8 or scan_count >= 63:
                    break
                continue
            stable_confirmed = 0
            revision = self._revisions.get(key, -1) + 1
            self._revisions[key] = revision
            self._latest_fingerprint[key] = fingerprint
            payload = {
                "level": self.level,
                "bi_idx": key[0],
                "direction": direction,
                "types": bsp_types,
                "signal_time": signal_time.isoformat(),
            }
            self.record(
                SubLevelSignalSnapshot(
                    signal_id=_stable_id("child", payload),
                    level=self.level,
                    bi_idx=key[0],
                    direction=direction,
                    bsp_types=bsp_types,
                    signal_time=signal_time,
                    bi_begin_time=begin_time,
                    bi_end_time=end_time,
                    available_at=available_at,
                    is_confirmed=confirmed,
                    revision=revision,
                )
            )
            if scan_count >= 63:
                break

    def record(self, snapshot: SubLevelSignalSnapshot) -> None:
        normalized = replace(
            snapshot,
            signal_time=market_timestamp(snapshot.signal_time),
            bi_begin_time=market_timestamp(snapshot.bi_begin_time),
            bi_end_time=market_timestamp(snapshot.bi_end_time),
            available_at=market_timestamp(snapshot.available_at),
        )
        if normalized not in self._history:
            self._history.append(normalized)
            self._history.sort(key=lambda item: market_timestamp(item.available_at))
            self._observation_times.append(market_timestamp(normalized.available_at))
            self.last_observed_at = max(
                filter(
                    lambda value: value is not None,
                    (self.last_observed_at, market_timestamp(normalized.available_at)),
                )
            )

    def matches(
        self,
        *,
        direction: str,
        window_begin: object,
        window_end: object,
        decision_time: object,
        accepted_types: tuple[str, ...],
    ) -> tuple[SubLevelSignalSnapshot, ...]:
        begin = market_timestamp(window_begin)
        end = market_timestamp(window_end)
        cutoff = market_timestamp(decision_time)
        accepted = set(accepted_types)
        latest: dict[str, SubLevelSignalSnapshot] = {}
        for snapshot in self._history:
            if market_timestamp(snapshot.available_at) > cutoff:
                continue
            if snapshot.direction != direction or not snapshot.is_confirmed:
                continue
            if not accepted.intersection(snapshot.bsp_types):
                continue
            signal_time = market_timestamp(snapshot.signal_time)
            if begin <= signal_time <= end:
                prior = latest.get(snapshot.signal_id)
                if prior is None or snapshot.revision > prior.revision:
                    latest[snapshot.signal_id] = snapshot
        return tuple(
            sorted(latest.values(), key=lambda item: market_timestamp(item.signal_time))
        )

    def has_future_match(
        self,
        *,
        direction: str,
        window_begin: object,
        window_end: object,
        decision_time: object,
        accepted_types: tuple[str, ...],
    ) -> bool:
        begin = market_timestamp(window_begin)
        end = market_timestamp(window_end)
        cutoff = market_timestamp(decision_time)
        accepted = set(accepted_types)
        return any(
            snapshot.direction == direction
            and snapshot.is_confirmed
            and bool(accepted.intersection(snapshot.bsp_types))
            and begin <= market_timestamp(snapshot.signal_time) <= end
            and market_timestamp(snapshot.available_at) > cutoff
            for snapshot in self._history
        )

    def last_observed_as_of(self, decision_time: object) -> pd.Timestamp | None:
        cutoff = market_timestamp(decision_time)
        values = [value for value in self._observation_times if value <= cutoff]
        return max(values) if values else None

    @property
    def history(self) -> tuple[SubLevelSignalSnapshot, ...]:
        return tuple(self._history)


class MultiLevelDecisionEngine:
    """Own level snapshots, enforce filters, and retain an audit per signal."""

    def __init__(
        self,
        *,
        enabled: bool,
        code: str,
        chan_config: dict[str, object],
        parent_kl_type: KL_TYPE,
        child_kl_type: KL_TYPE,
        parent_level_name: str,
        child_level_name: str,
        require_parent_direction: bool = True,
        require_confirmed_parent: bool = True,
        require_child_confirmation: bool = True,
        accepted_child_bsp_types: tuple[str, ...] = ("1", "1p", "2"),
        parent_frame: pd.DataFrame | None = None,
        child_frame: pd.DataFrame | None = None,
    ) -> None:
        self._code = code
        self._chan_config = dict(chan_config)
        self._parent_level_name = parent_level_name
        self._child_level_name = child_level_name
        self._require_confirmed_parent = require_confirmed_parent
        self.enabled = enabled
        self.require_parent_direction = require_parent_direction
        self.require_child_confirmation = require_child_confirmation
        self.accepted_child_bsp_types = accepted_child_bsp_types
        self.parent_kl_type = parent_kl_type
        self.child_kl_type = child_kl_type
        self.parent_tracker = ParentStructureTracker(
            level=parent_level_name,
            require_confirmed=require_confirmed_parent,
        )
        self.child_tracker = SubLevelSignalTracker(level=child_level_name)
        self._parent_chan = _new_level_chan(
            code, parent_kl_type, CChanConfig(dict(chan_config))
        )
        self._child_chan = _new_level_chan(
            code, child_kl_type, CChanConfig(dict(chan_config))
        )
        self._parent_frame = _prepare_optional_frame(parent_frame)
        self._child_frame = _prepare_optional_frame(child_frame)
        self._parent_cursor = 0
        self._child_cursor = 0
        self._audit: list[MultiLevelDecisionContext] = []

    def reset_contract_state(
        self,
        start_time: object | None = None,
        active_symbol: str | None = None,
    ) -> None:
        """Reset both structural levels at an actual-contract boundary."""
        self.parent_tracker = ParentStructureTracker(
            level=self._parent_level_name,
            require_confirmed=self._require_confirmed_parent,
        )
        self.child_tracker = SubLevelSignalTracker(level=self._child_level_name)
        self._parent_chan = _new_level_chan(
            self._code,
            self.parent_kl_type,
            CChanConfig(dict(self._chan_config)),
        )
        self._child_chan = _new_level_chan(
            self._code,
            self.child_kl_type,
            CChanConfig(dict(self._chan_config)),
        )
        self._parent_cursor = _contract_cursor(
            self._parent_frame,
            start_time,
            active_symbol,
        )
        self._child_cursor = _contract_cursor(
            self._child_frame,
            start_time,
            active_symbol,
        )

    def advance_to(self, decision_time: object) -> None:
        if not self.enabled:
            return
        cutoff = market_timestamp(decision_time)
        self._parent_cursor = self._advance_frame(
            frame=self._parent_frame,
            cursor=self._parent_cursor,
            cutoff=cutoff,
            chan=self._parent_chan,
            kl_type=self.parent_kl_type,
            observer=self.parent_tracker.observe,
        )
        self._child_cursor = self._advance_frame(
            frame=self._child_frame,
            cursor=self._child_cursor,
            cutoff=cutoff,
            chan=self._child_chan,
            kl_type=self.child_kl_type,
            observer=self.child_tracker.observe,
        )

    def observe_parent_bar(self, klu, *, available_at: object | None = None) -> None:
        timestamp = available_at if available_at is not None else klu_timestamp(klu)
        self._parent_chan.trigger_load({self.parent_kl_type: [klu]})
        self.parent_tracker.observe(self._parent_chan, observed_at=timestamp)

    def observe_child_bar(self, klu, *, available_at: object | None = None) -> None:
        timestamp = available_at if available_at is not None else klu_timestamp(klu)
        self._child_chan.trigger_load({self.child_kl_type: [klu]})
        self.child_tracker.observe(self._child_chan, observed_at=timestamp)

    def apply(
        self,
        intent,
        *,
        current_chan: CChan,
        decision_time: object,
        lv_idx: int = 0,
    ):
        if not self.enabled:
            return intent
        context = self.assess(
            intent,
            current_chan=current_chan,
            decision_time=decision_time,
            lv_idx=lv_idx,
        )
        self._audit.append(context)
        updated = replace(intent, multi_level=context)
        if not intent.accepted or context.accepted:
            return updated
        reasons = tuple(dict.fromkeys((*intent.decision.reason_codes, *context.reason_codes)))
        decision = replace(
            intent.decision,
            accepted=False,
            reason_codes=reasons,
            entry_price_hint=None,
            position_size_hint=None,
        )
        return replace(updated, decision=decision)

    def assess(
        self,
        intent,
        *,
        current_chan: CChan,
        decision_time: object,
        lv_idx: int = 0,
    ) -> MultiLevelDecisionContext:
        cutoff = market_timestamp(decision_time)
        direction = _intent_direction(intent)
        is_entry = intent.signal.target_position != 0
        reasons: list[str] = []

        parent = self.parent_tracker.as_of(cutoff)
        parent_action = "bypass" if not is_entry else "pass"
        if is_entry and self.require_parent_direction:
            if parent is None:
                parent_action = "reject"
                if any(
                    market_timestamp(item.available_at) > cutoff
                    for item in self.parent_tracker.history
                ):
                    reasons.append("parent_not_available_at_decision")
                else:
                    reasons.append("parent_structure_unavailable")
            elif not parent.is_confirmed:
                parent_action = "reject"
                reasons.append("parent_structure_unconfirmed")
            elif (
                direction == "long" and parent.direction != ParentDirection.BULLISH
            ) or (
                direction == "short" and parent.direction != ParentDirection.BEARISH
            ):
                parent_action = "reject"
                reasons.append("parent_direction_conflict")

        window_begin, window_end = _current_bi_window(intent, current_chan, lv_idx)
        child_matches: tuple[SubLevelSignalSnapshot, ...] = ()
        child_confirmed = not (is_entry and self.require_child_confirmation)
        child_observed_at = self.child_tracker.last_observed_as_of(cutoff)
        if is_entry and self.require_child_confirmation:
            if window_begin is None or window_end is None:
                reasons.append("current_bi_time_window_unavailable")
            elif child_observed_at is None:
                if self.child_tracker.has_future_match(
                    direction=direction,
                    window_begin=window_begin,
                    window_end=window_end,
                    decision_time=cutoff,
                    accepted_types=self.accepted_child_bsp_types,
                ):
                    reasons.append("sublevel_confirmation_not_available_at_decision")
                else:
                    reasons.append("sublevel_data_unavailable")
            else:
                child_matches = self.child_tracker.matches(
                    direction=direction,
                    window_begin=window_begin,
                    window_end=window_end,
                    decision_time=cutoff,
                    accepted_types=self.accepted_child_bsp_types,
                )
                child_confirmed = bool(child_matches)
                if not child_confirmed:
                    if self.child_tracker.has_future_match(
                        direction=direction,
                        window_begin=window_begin,
                        window_end=window_end,
                        decision_time=cutoff,
                        accepted_types=self.accepted_child_bsp_types,
                    ):
                        reasons.append("sublevel_confirmation_not_available_at_decision")
                    else:
                        reasons.append("sublevel_confirmation_missing")

        time_honest = (
            (
                parent is None
                or (
                    market_timestamp(parent.structure_end_time)
                    <= market_timestamp(parent.available_at)
                    <= cutoff
                )
            )
            and all(
                market_timestamp(match.signal_time)
                <= market_timestamp(match.available_at)
                <= cutoff
                for match in child_matches
            )
        )
        if not time_honest:
            reasons.append("multi_level_future_leak")
        reasons_tuple = tuple(dict.fromkeys(reasons))
        return MultiLevelDecisionContext(
            decision_time=cutoff,
            accepted=not reasons_tuple,
            time_honest=time_honest,
            reason_codes=reasons_tuple,
            parent_required=is_entry and self.require_parent_direction,
            parent_snapshot=parent,
            parent_action=parent_action,
            child_required=is_entry and self.require_child_confirmation,
            child_window_begin=window_begin,
            child_window_end=window_end,
            child_confirmed=child_confirmed,
            child_matches=child_matches,
            parent_last_observed_at=self.parent_tracker.last_observed_as_of(cutoff),
            child_last_observed_at=child_observed_at,
        )

    @property
    def audit(self) -> tuple[MultiLevelDecisionContext, ...]:
        return tuple(self._audit)

    @property
    def readiness(self) -> MultiLevelReadiness:
        parent_level = self._parent_chan[0]
        child_level = self._child_chan[0]
        return MultiLevelReadiness(
            parent_confirmed_segments=sum(
                bool(segment.is_sure) for segment in parent_level.seg_list
            ),
            child_confirmed_bis=sum(bool(bi.is_sure) for bi in child_level.bi_list),
        )

    @staticmethod
    def _advance_frame(
        *,
        frame: pd.DataFrame | None,
        cursor: int,
        cutoff: pd.Timestamp,
        chan: CChan,
        kl_type: KL_TYPE,
        observer,
    ) -> int:
        if frame is None:
            return cursor
        while cursor < len(frame):
            row = frame.iloc[cursor]
            available_at = market_timestamp(row["datetime"])
            if available_at > cutoff:
                break
            klu = row_to_klu(row, kl_type=kl_type)
            chan.trigger_load({kl_type: [klu]})
            observer(chan, observed_at=available_at)
            cursor += 1
        return cursor


def make_multi_level_engine(
    config,
    *,
    parent_frame: pd.DataFrame | None = None,
    child_frame: pd.DataFrame | None = None,
) -> MultiLevelDecisionEngine | None:
    params = config.multi_level
    if not params.enabled:
        return None
    parent_rank = _level_rank(params.parent_kl_type)
    current_rank = _level_rank(config.kl_type)
    child_rank = _level_rank(params.child_kl_type)
    if not parent_rank > current_rank > child_rank:
        raise ValueError(
            "multi-level order must be parent > decision level > child: "
            f"{params.parent_kl_type} > {config.kl_type} > {params.child_kl_type}"
        )
    return MultiLevelDecisionEngine(
        enabled=True,
        code=config.code,
        chan_config=config.chan.to_dict(),
        parent_kl_type=resolve_kl_type(params.parent_kl_type),
        child_kl_type=resolve_kl_type(params.child_kl_type),
        parent_level_name=params.parent_kl_type,
        child_level_name=params.child_kl_type,
        require_parent_direction=params.require_parent_direction,
        require_confirmed_parent=params.require_confirmed_parent,
        require_child_confirmation=params.require_child_confirmation,
        accepted_child_bsp_types=params.accepted_child_bsp_types,
        parent_frame=parent_frame,
        child_frame=child_frame,
    )


def resolve_kl_type(value: str) -> KL_TYPE:
    try:
        return _KL_TYPE_MAP[value]
    except KeyError as exc:
        raise ValueError(f"unsupported multi-level K-line type: {value}") from exc


def _level_rank(value: str) -> int:
    ranks = {
        "K_1M": 1,
        "K_5M": 5,
        "K_15M": 15,
        "K_30M": 30,
        "K_60M": 60,
        "K_DAY": 1_440,
        "K_WEEK": 10_080,
        "K_MON": 43_200,
    }
    try:
        return ranks[value]
    except KeyError as exc:
        raise ValueError(f"unsupported multi-level K-line type: {value}") from exc


def _contract_cursor(
    frame: pd.DataFrame | None,
    start_time: object | None,
    active_symbol: str | None,
) -> int:
    if frame is None or start_time is None:
        return 0
    if active_symbol is not None and "active_symbol" in frame.columns:
        matches = frame.index[frame["active_symbol"].astype(str) == active_symbol]
        if len(matches):
            return int(matches[0])
    cutoff = market_timestamp(start_time)
    for idx, value in enumerate(frame["datetime"]):
        if market_timestamp(value) >= cutoff:
            return idx
    return len(frame)


def market_timestamp(value: object) -> pd.Timestamp:
    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(
        value, (str, pd.Timestamp)
    ):
        try:
            value = pd.Timestamp(
                year=int(value.year),
                month=int(value.month),
                day=int(value.day),
                hour=int(getattr(value, "hour", 0)),
                minute=int(getattr(value, "minute", 0)),
                second=int(getattr(value, "second", 0)),
            )
        except (TypeError, ValueError):
            pass
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("Asia/Shanghai")
    return timestamp.tz_convert("Asia/Shanghai")


def klu_timestamp(klu) -> pd.Timestamp:
    return market_timestamp(klu.time)


def _new_level_chan(code: str, kl_type: KL_TYPE, chan_config: CChanConfig) -> CChan:
    return CChan(
        code=code,
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[kl_type],
        config=chan_config,
        autype=AUTYPE.NONE,
    )


def _prepare_optional_frame(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None:
        return None
    return prepare_ohlc_frame(frame)


def _intent_direction(intent) -> str:
    if intent.event is not None:
        return intent.event.direction.value
    return "long" if intent.signal.target_position > 0 else "short"


def _current_bi_window(intent, current_chan: CChan, lv_idx: int) -> tuple[object | None, object | None]:
    bi_idx = intent.event.bi_idx if intent.event is not None else intent.signal.bsp_bi_idx
    if bi_idx is None or int(bi_idx) < 0:
        return None, None
    bi_list = current_chan[lv_idx].bi_list
    if int(bi_idx) >= len(bi_list):
        return None, None
    bi = bi_list[int(bi_idx)]
    return klu_timestamp(bi.get_begin_klu()), klu_timestamp(bi.get_end_klu())


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:16]}"
