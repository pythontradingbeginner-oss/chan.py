"""Auditable Qingpai regime classification and same-level decomposition.

The module is deliberately outside the chan.py structure engine.  It consumes
immutable snapshots of strokes and centers, so classification rules can evolve
without rewriting Bi/Seg/ZS calculations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Iterable, Sequence


class QingpaiRegime(StrEnum):
    """Qingpai trend and consolidation subtypes."""

    UNCLASSIFIED = "unclassified"
    CONSOLIDATION = "consolidation"
    EXTENSION = "extension"
    EXPANSION = "expansion"
    EXPANDED = "expanded"
    TREND = "trend"


class StructureDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class DecompositionLifecycle(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TransitionKind(StrEnum):
    OPEN = "open"
    REVISE = "revise"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class StrokeSnapshot:
    idx: int
    direction: StructureDirection
    begin_price: float
    end_price: float
    low: float
    high: float
    is_sure: bool = True

    def __post_init__(self) -> None:
        if self.idx < 0:
            raise ValueError("stroke idx must be >= 0")
        if self.high < self.low:
            raise ValueError("stroke high must be >= low")


@dataclass(frozen=True, slots=True)
class CenterSnapshot:
    """Point-in-time center geometry used by the strategy-side classifier."""

    center_id: str
    entry_bi_idx: int
    begin_bi_idx: int
    end_bi_idx: int
    direction: StructureDirection
    start_price: float
    low: float
    high: float
    peak_low: float
    peak_high: float
    is_sure: bool
    is_valid: bool = True

    def __post_init__(self) -> None:
        if self.begin_bi_idx <= self.entry_bi_idx:
            raise ValueError("center must begin after its entry stroke")
        if self.end_bi_idx < self.begin_bi_idx:
            raise ValueError("center end must not precede begin")
        if self.high < self.low:
            raise ValueError("center high must be >= low")
        if self.peak_high < self.peak_low:
            raise ValueError("center peak_high must be >= peak_low")

    @property
    def stroke_count(self) -> int:
        return self.end_bi_idx - self.begin_bi_idx + 1


@dataclass(frozen=True, slots=True)
class DecompositionObservation:
    observed_at: object
    available_at: object
    centers: tuple[CenterSnapshot, ...]
    strokes: tuple[StrokeSnapshot, ...]
    level: str = "bi"


@dataclass(frozen=True, slots=True)
class DecompositionSnapshot:
    decomposition_id: str
    revision: int
    regime: QingpaiRegime
    direction: StructureDirection
    lifecycle: DecompositionLifecycle
    observed_at: object
    available_at: object
    start_bi_idx: int
    end_bi_idx: int
    centers: tuple[CenterSnapshot, ...]
    is_confirmed: bool
    upgrade_candidate: bool
    reason_codes: tuple[str, ...]

    @property
    def center_ids(self) -> tuple[str, ...]:
        return tuple(center.center_id for center in self.centers)

    def semantic_fingerprint(self) -> str:
        payload = {
            "regime": self.regime,
            "direction": self.direction,
            "lifecycle": self.lifecycle,
            "start_bi_idx": self.start_bi_idx,
            "end_bi_idx": self.end_bi_idx,
            "centers": [asdict(center) for center in self.centers],
            "is_confirmed": self.is_confirmed,
            "upgrade_candidate": self.upgrade_candidate,
            "reason_codes": self.reason_codes,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def to_dict(self) -> dict[str, object]:
        return {
            "decomposition_id": self.decomposition_id,
            "revision": self.revision,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "lifecycle": self.lifecycle.value,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "start_bi_idx": self.start_bi_idx,
            "end_bi_idx": self.end_bi_idx,
            "center_ids": self.center_ids,
            "is_confirmed": self.is_confirmed,
            "upgrade_candidate": self.upgrade_candidate,
            "reason_codes": self.reason_codes,
            "fingerprint": self.semantic_fingerprint(),
            "centers": json.dumps(
                [asdict(center) for center in self.centers],
                ensure_ascii=False,
                default=str,
            ),
        }


@dataclass(frozen=True, slots=True)
class DecompositionTransition:
    transition_id: str
    sequence: int
    decomposition_id: str
    revision: int
    kind: TransitionKind
    observed_at: object
    available_at: object
    from_regime: QingpaiRegime | None
    to_regime: QingpaiRegime
    from_lifecycle: DecompositionLifecycle | None
    to_lifecycle: DecompositionLifecycle
    trigger: str
    rule_ids: tuple[str, ...]
    snapshot_fingerprint: str
    center_ids: tuple[str, ...]
    direction: StructureDirection
    is_confirmed: bool
    upgrade_candidate: bool
    reason_codes: tuple[str, ...]
    centers: tuple[CenterSnapshot, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "transition_id": self.transition_id,
            "sequence": self.sequence,
            "decomposition_id": self.decomposition_id,
            "revision": self.revision,
            "kind": self.kind.value,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "from_regime": self.from_regime.value if self.from_regime else None,
            "to_regime": self.to_regime.value,
            "from_lifecycle": (
                self.from_lifecycle.value if self.from_lifecycle else None
            ),
            "to_lifecycle": self.to_lifecycle.value,
            "trigger": self.trigger,
            "rule_ids": self.rule_ids,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "center_ids": self.center_ids,
            "direction": self.direction.value,
            "is_confirmed": self.is_confirmed,
            "upgrade_candidate": self.upgrade_candidate,
            "reason_codes": self.reason_codes,
            "centers": json.dumps(
                [asdict(center) for center in self.centers],
                ensure_ascii=False,
                default=str,
            ),
        }


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    regime: QingpaiRegime
    centers: tuple[CenterSnapshot, ...]
    end_bi_idx: int
    is_confirmed: bool
    upgrade_candidate: bool
    reason_codes: tuple[str, ...]


class QingpaiStateMachine:
    """Append-only state machine for one open same-level decomposition."""

    def __init__(self, anchor: CenterSnapshot, *, level: str = "bi") -> None:
        self.anchor = anchor
        self.level = level
        self._current: DecompositionSnapshot | None = None
        self._transitions: list[DecompositionTransition] = []

    def update(self, observation: DecompositionObservation) -> DecompositionSnapshot:
        if self._current is not None and self._current.lifecycle == DecompositionLifecycle.CLOSED:
            return self._current

        classified = classify_observation(
            observation,
            anchor_center_id=self.anchor.center_id,
            direction=self.anchor.direction,
        )
        revision = 0 if self._current is None else self._current.revision + 1
        candidate = DecompositionSnapshot(
            decomposition_id=_decomposition_id(self.level, self.anchor),
            revision=revision,
            regime=classified.regime,
            direction=self.anchor.direction,
            lifecycle=DecompositionLifecycle.OPEN,
            observed_at=observation.observed_at,
            available_at=observation.available_at,
            start_bi_idx=self.anchor.entry_bi_idx,
            end_bi_idx=classified.end_bi_idx,
            centers=classified.centers,
            is_confirmed=classified.is_confirmed,
            upgrade_candidate=classified.upgrade_candidate,
            reason_codes=classified.reason_codes,
        )
        if (
            self._current is not None
            and candidate.semantic_fingerprint() == self._current.semantic_fingerprint()
        ):
            return self._current

        previous = self._current
        self._current = candidate
        self._append_transition(
            previous=previous,
            current=candidate,
            kind=TransitionKind.OPEN if previous is None else TransitionKind.REVISE,
            trigger=_revision_trigger(previous, candidate),
        )
        return candidate

    def close(
        self,
        *,
        observed_at: object,
        available_at: object,
        trigger: str,
    ) -> DecompositionSnapshot | None:
        if self._current is None:
            return None
        if self._current.lifecycle == DecompositionLifecycle.CLOSED:
            return self._current

        previous = self._current
        self._current = replace(
            previous,
            revision=previous.revision + 1,
            lifecycle=DecompositionLifecycle.CLOSED,
            observed_at=observed_at,
            available_at=available_at,
            reason_codes=previous.reason_codes + (trigger,),
        )
        self._append_transition(
            previous=previous,
            current=self._current,
            kind=TransitionKind.CLOSE,
            trigger=trigger,
        )
        return self._current

    def _append_transition(
        self,
        *,
        previous: DecompositionSnapshot | None,
        current: DecompositionSnapshot,
        kind: TransitionKind,
        trigger: str,
    ) -> None:
        sequence = len(self._transitions)
        token = (
            f"{current.decomposition_id}:{current.revision}:{kind.value}:"
            f"{current.semantic_fingerprint()}"
        )
        transition_id = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        self._transitions.append(
            DecompositionTransition(
                transition_id=transition_id,
                sequence=sequence,
                decomposition_id=current.decomposition_id,
                revision=current.revision,
                kind=kind,
                observed_at=current.observed_at,
                available_at=current.available_at,
                from_regime=previous.regime if previous else None,
                to_regime=current.regime,
                from_lifecycle=previous.lifecycle if previous else None,
                to_lifecycle=current.lifecycle,
                trigger=trigger,
                rule_ids=("QP-DECOMP-001", "QP-DECOMP-002", "QP-REGIME-001"),
                snapshot_fingerprint=current.semantic_fingerprint(),
                center_ids=current.center_ids,
                direction=current.direction,
                is_confirmed=current.is_confirmed,
                upgrade_candidate=current.upgrade_candidate,
                reason_codes=current.reason_codes,
                centers=current.centers,
            )
        )

    @property
    def current(self) -> DecompositionSnapshot | None:
        return self._current

    @property
    def transitions(self) -> tuple[DecompositionTransition, ...]:
        return tuple(self._transitions)


class QingpaiDecomposer:
    """Track consecutive machines and archive closed snapshots immutably."""

    def __init__(self, *, level: str = "bi") -> None:
        self.level = level
        self._machine: QingpaiStateMachine | None = None
        self._closed: list[DecompositionSnapshot] = []
        self._transitions: list[DecompositionTransition] = []
        self._machine_transition_count = 0
        self._next_begin_bi_idx = 0
        self._last_structure_signature: tuple[object, ...] | None = None

    def update(self, observation: DecompositionObservation) -> DecompositionSnapshot | None:
        centers = tuple(
            center
            for center in observation.centers
            if center.begin_bi_idx >= self._next_begin_bi_idx
        )
        if not centers:
            return self.current

        if self._machine is None:
            anchor = next((center for center in centers if center.is_valid), None)
            if anchor is None:
                return None
            self._open_machine(anchor)
            centers = tuple(
                center for center in centers if center.begin_bi_idx >= anchor.begin_bi_idx
            )

        assert self._machine is not None
        boundary = _find_boundary(self._machine.anchor, centers, observation.strokes)
        if boundary is not None:
            boundary_idx, trigger, next_center = boundary
            pre_boundary = replace(
                observation,
                centers=tuple(c for c in centers if c.begin_bi_idx < boundary_idx),
                strokes=tuple(s for s in observation.strokes if s.idx <= boundary_idx),
            )
            if pre_boundary.centers:
                self._machine.update(pre_boundary)
                self._sync_machine_transitions()
            closed = self._machine.close(
                observed_at=observation.observed_at,
                available_at=observation.available_at,
                trigger=trigger,
            )
            self._sync_machine_transitions()
            if closed is not None:
                self._closed.append(closed)
            self._machine = None
            self._machine_transition_count = 0
            self._next_begin_bi_idx = boundary_idx

            if next_center is None or not next_center.is_valid:
                return None
            self._open_machine(next_center)
            centers = tuple(c for c in centers if c.begin_bi_idx >= boundary_idx)

        assert self._machine is not None
        scoped = replace(observation, centers=centers)
        snapshot = self._machine.update(scoped)
        self._sync_machine_transitions()
        return snapshot

    def update_from_chan(
        self,
        chan,
        *,
        observed_at: object,
        lv_idx: int = 0,
    ) -> DecompositionSnapshot | None:
        signature = _chan_structure_signature(chan, lv_idx=lv_idx, level=self.level)
        if signature == self._last_structure_signature:
            return self.current
        self._last_structure_signature = signature
        observation = observation_from_chan(
            chan,
            observed_at=observed_at,
            available_at=observed_at,
            lv_idx=lv_idx,
            level=self.level,
            min_line_idx=max(0, self._next_begin_bi_idx - 1),
        )
        return self.update(observation)

    def _open_machine(self, anchor: CenterSnapshot) -> None:
        self._machine = QingpaiStateMachine(anchor, level=self.level)
        self._machine_transition_count = 0

    def _sync_machine_transitions(self) -> None:
        if self._machine is None:
            return
        latest = self._machine.transitions
        for transition in latest[self._machine_transition_count :]:
            self._transitions.append(
                replace(transition, sequence=len(self._transitions))
            )
        self._machine_transition_count = len(latest)

    @property
    def current(self) -> DecompositionSnapshot | None:
        return self._machine.current if self._machine is not None else None

    @property
    def closed_states(self) -> tuple[DecompositionSnapshot, ...]:
        return tuple(self._closed)

    @property
    def transitions(self) -> tuple[DecompositionTransition, ...]:
        return tuple(self._transitions)


def classify_observation(
    observation: DecompositionObservation,
    *,
    anchor_center_id: str | None = None,
    direction: StructureDirection | None = None,
) -> RegimeClassification:
    """Classify one point-in-time observation with deterministic rules."""
    centers = tuple(sorted(observation.centers, key=_center_sort_key))
    if anchor_center_id is not None:
        anchor_pos = next(
            (i for i, center in enumerate(centers) if center.center_id == anchor_center_id),
            None,
        )
        if anchor_pos is not None:
            centers = centers[anchor_pos:]
    if direction is None and centers:
        direction = centers[0].direction
    if direction is not None:
        centers = _same_direction_prefix(centers, direction)

    if not centers:
        return RegimeClassification(
            regime=QingpaiRegime.UNCLASSIFIED,
            centers=(),
            end_bi_idx=-1,
            is_confirmed=False,
            upgrade_candidate=False,
            reason_codes=("center_unavailable",),
        )

    if any(not center.is_valid for center in centers):
        return RegimeClassification(
            regime=QingpaiRegime.UNCLASSIFIED,
            centers=centers,
            end_bi_idx=max(center.end_bi_idx for center in centers),
            is_confirmed=False,
            upgrade_candidate=False,
            reason_codes=("center_entry_start_invalidated",),
        )

    end_idx = max(
        centers[-1].end_bi_idx,
        max((stroke.idx for stroke in observation.strokes), default=-1),
    )
    confirmed = all(center.is_sure for center in centers)
    upgrade_candidate = any(center.stroke_count >= 9 for center in centers)

    if len(centers) == 1:
        center = centers[0]
        if center.stroke_count > 3:
            return RegimeClassification(
                QingpaiRegime.EXTENSION,
                centers,
                end_idx,
                confirmed,
                upgrade_candidate,
                ("single_center_extended",),
            )
        return RegimeClassification(
            QingpaiRegime.CONSOLIDATION,
            centers,
            end_idx,
            confirmed,
            upgrade_candidate,
            ("single_standard_center",),
        )

    relations = [
        _center_relation(left, right, centers[0].direction)
        for left, right in zip(centers, centers[1:])
    ]
    if any(relation == "overlapping_center" for relation in relations):
        return RegimeClassification(
            QingpaiRegime.EXTENSION,
            centers,
            end_idx,
            confirmed,
            upgrade_candidate,
            ("center_zones_overlap",),
        )
    if any(relation == "wrong_order" for relation in relations):
        return RegimeClassification(
            QingpaiRegime.UNCLASSIFIED,
            centers,
            end_idx,
            False,
            upgrade_candidate,
            ("same_direction_centers_not_progressing",),
        )
    if all(relation == "trend" for relation in relations):
        return RegimeClassification(
            QingpaiRegime.TREND,
            centers,
            end_idx,
            confirmed,
            upgrade_candidate,
            ("center_swing_ranges_separated",),
        )

    previous, latest = centers[-2], centers[-1]
    if _has_expanded_retrace(previous, latest, observation.strokes):
        return RegimeClassification(
            QingpaiRegime.EXPANDED,
            centers,
            end_idx,
            confirmed,
            True,
            ("second_center_retraced_before_directional_break",),
        )
    return RegimeClassification(
        QingpaiRegime.EXPANSION,
        centers,
        end_idx,
        confirmed,
        True,
        ("center_zones_separated_swing_ranges_overlap",),
    )


def observation_from_chan(
    chan,
    *,
    observed_at: object,
    available_at: object | None = None,
    lv_idx: int = 0,
    level: str = "bi",
    min_line_idx: int = 0,
) -> DecompositionObservation:
    """Freeze the current chan.py structures without mutating core objects."""
    kl_data = chan[lv_idx]
    if level == "bi":
        line_list = kl_data.bi_list
        center_list = kl_data.zs_list
    elif level == "seg":
        line_list = kl_data.seg_list
        center_list = kl_data.segzs_list
    else:
        raise ValueError("level must be 'bi' or 'seg'")

    raw_lines = tuple(line_list[min_line_idx:])
    strokes = tuple(_stroke_snapshot(line) for line in raw_lines)
    by_idx = {int(line.idx): line for line in raw_lines}
    centers: list[CenterSnapshot] = []
    for zs in _atomic_centers(center_list):
        if int(zs.begin_bi.idx) - 1 < min_line_idx:
            continue
        center = _center_snapshot(zs, by_idx)
        if center is not None:
            centers.append(center)
    centers = list(_deduplicate_centers(centers))
    return DecompositionObservation(
        observed_at=observed_at,
        available_at=observed_at if available_at is None else available_at,
        centers=tuple(sorted(centers, key=_center_sort_key)),
        strokes=strokes,
        level=level,
    )


def _stroke_snapshot(line) -> StrokeSnapshot:
    direction = (
        StructureDirection.BULLISH if line.is_up() else StructureDirection.BEARISH
    )
    return StrokeSnapshot(
        idx=int(line.idx),
        direction=direction,
        begin_price=float(line.get_begin_val()),
        end_price=float(line.get_end_val()),
        low=float(line._low()),
        high=float(line._high()),
        is_sure=bool(line.is_sure),
    )


def _chan_structure_signature(
    chan,
    *,
    lv_idx: int,
    level: str,
) -> tuple[object, ...]:
    kl_data = chan[lv_idx]
    if level == "bi":
        line_list = kl_data.bi_list
        center_list = kl_data.zs_list
    elif level == "seg":
        line_list = kl_data.seg_list
        center_list = kl_data.segzs_list
    else:
        raise ValueError("level must be 'bi' or 'seg'")

    line_tail = tuple(
        _line_signature(line)
        for line in line_list[max(0, len(line_list) - 2) :]
    )
    center_count = len(center_list)
    center_tail = tuple(
        _center_source_signature(center)
        for center in center_list[max(0, center_count - 2) :]
    )
    return len(line_list), line_tail, center_count, center_tail


def _line_signature(line) -> tuple[object, ...]:
    return (
        int(line.idx),
        bool(line.is_sure),
        float(line.get_begin_val()),
        float(line.get_end_val()),
        float(line._low()),
        float(line._high()),
    )


def _center_source_signature(center) -> tuple[object, ...]:
    children = tuple(getattr(center, "sub_zs_lst", ()) or ())
    child_tail = tuple(
        _center_source_signature(child)
        for child in children[max(0, len(children) - 2) :]
    )
    return (
        int(center.begin_bi.idx),
        int(center.end_bi.idx),
        bool(center.is_sure),
        float(center.low),
        float(center.high),
        float(center.peak_low),
        float(center.peak_high),
        len(children),
        child_tail,
    )


def _atomic_centers(center_list) -> Iterable[object]:
    for center in center_list:
        children = tuple(getattr(center, "sub_zs_lst", ()) or ())
        if children:
            for child in children:
                yield from _atomic_centers((child,))
        else:
            yield center


def _center_snapshot(zs, by_idx: dict[int, object]) -> CenterSnapshot | None:
    begin_idx = int(zs.begin_bi.idx)
    end_idx = int(zs.end_bi.idx)
    entry_idx = begin_idx - 1
    entry = getattr(zs, "bi_in", None) or by_idx.get(entry_idx)
    if entry is None:
        return None
    direction = (
        StructureDirection.BULLISH if entry.is_up() else StructureDirection.BEARISH
    )
    center_id = f"{direction.value}:{entry_idx}:{begin_idx}"
    enclosed = [
        line
        for idx, line in by_idx.items()
        if entry_idx <= idx <= end_idx
    ]
    if direction == StructureDirection.BULLISH:
        is_valid = all(float(line._low()) >= float(entry.get_begin_val()) for line in enclosed)
    else:
        is_valid = all(float(line._high()) <= float(entry.get_begin_val()) for line in enclosed)
    return CenterSnapshot(
        center_id=center_id,
        entry_bi_idx=entry_idx,
        begin_bi_idx=begin_idx,
        end_bi_idx=end_idx,
        direction=direction,
        start_price=float(entry.get_begin_val()),
        low=float(zs.low),
        high=float(zs.high),
        peak_low=float(zs.peak_low),
        peak_high=float(zs.peak_high),
        is_sure=bool(zs.is_sure),
        is_valid=is_valid,
    )


def _deduplicate_centers(
    centers: Sequence[CenterSnapshot],
) -> tuple[CenterSnapshot, ...]:
    latest: dict[str, CenterSnapshot] = {}
    for center in centers:
        previous = latest.get(center.center_id)
        if previous is None or center.end_bi_idx >= previous.end_bi_idx:
            latest[center.center_id] = center
    return tuple(latest.values())


def _same_direction_prefix(
    centers: Sequence[CenterSnapshot],
    direction: StructureDirection,
) -> tuple[CenterSnapshot, ...]:
    result: list[CenterSnapshot] = []
    for center in centers:
        if center.direction != direction:
            if center.is_sure:
                break
            continue
        result.append(center)
    return tuple(result)


def _center_relation(
    left: CenterSnapshot,
    right: CenterSnapshot,
    direction: StructureDirection,
) -> str:
    if _overlap(left.low, left.high, right.low, right.high):
        return "overlapping_center"
    if direction == StructureDirection.BULLISH:
        if right.low <= left.high:
            return "wrong_order"
        swing_separated = right.peak_low > left.peak_high
    else:
        if right.high >= left.low:
            return "wrong_order"
        swing_separated = right.peak_high < left.peak_low
    return "trend" if swing_separated else "expansion"


def _has_expanded_retrace(
    previous: CenterSnapshot,
    latest: CenterSnapshot,
    strokes: Sequence[StrokeSnapshot],
) -> bool:
    post = sorted(
        (stroke for stroke in strokes if stroke.idx > latest.end_bi_idx),
        key=lambda stroke: stroke.idx,
    )
    if latest.direction == StructureDirection.BULLISH:
        for stroke in post:
            if stroke.high > latest.peak_high:
                return False
            if stroke.low <= previous.high and stroke.low >= latest.start_price:
                return True
        return False
    for stroke in post:
        if stroke.low < latest.peak_low:
            return False
        if stroke.high >= previous.low and stroke.high <= latest.start_price:
            return True
    return False


def _find_boundary(
    anchor: CenterSnapshot,
    centers: Sequence[CenterSnapshot],
    strokes: Sequence[StrokeSnapshot],
) -> tuple[int, str, CenterSnapshot | None] | None:
    opposing = next(
        (
            center
            for center in centers
            if center.begin_bi_idx > anchor.begin_bi_idx
            and center.direction != anchor.direction
            and center.is_sure
            and center.is_valid
        ),
        None,
    )
    invalidating = next(
        (
            stroke
            for stroke in strokes
            if stroke.idx > anchor.end_bi_idx
            and stroke.is_sure
            and (
                (
                    anchor.direction == StructureDirection.BULLISH
                    and stroke.low < anchor.start_price
                )
                or (
                    anchor.direction == StructureDirection.BEARISH
                    and stroke.high > anchor.start_price
                )
            )
        ),
        None,
    )

    choices: list[tuple[int, str, CenterSnapshot | None]] = []
    if opposing is not None:
        choices.append((opposing.begin_bi_idx, "opposite_center_confirmed", opposing))
    if invalidating is not None:
        next_center = next(
            (
                c
                for c in centers
                if c.begin_bi_idx >= invalidating.idx and c.is_valid
            ),
            None,
        )
        choices.append((invalidating.idx, "entry_start_invalidated", next_center))
    return min(choices, key=lambda item: item[0]) if choices else None


def _revision_trigger(
    previous: DecompositionSnapshot | None,
    current: DecompositionSnapshot,
) -> str:
    if previous is None:
        return "first_center_observed"
    if previous.regime != current.regime:
        return f"regime_changed:{previous.regime.value}->{current.regime.value}"
    if previous.is_confirmed != current.is_confirmed:
        return "structure_confirmation_changed"
    if previous.center_ids != current.center_ids:
        return "center_set_changed"
    return "open_structure_revised"


def _decomposition_id(level: str, anchor: CenterSnapshot) -> str:
    return f"{level}:{anchor.direction.value}:{anchor.entry_bi_idx}"


def _center_sort_key(center: CenterSnapshot) -> tuple[int, int, str]:
    return center.begin_bi_idx, center.end_bi_idx, center.center_id


def _overlap(low1: float, high1: float, low2: float, high2: float) -> bool:
    return max(low1, low2) <= min(high1, high2)
