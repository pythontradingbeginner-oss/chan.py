"""Qingpai MACD area, peak and price-force confirmation.

The analyzer consumes point-in-time CBi objects.  MACD area is primary, peak
height resolves near ties, and price advance/slope is the final structural
check.  It never replaces center direction or the BSP structure itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite

from Common.CEnum import MACD_ALGO

from .multi_level import market_timestamp


class MomentumStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MomentumConfirmation:
    observed_at: object
    available_at: object
    status: MomentumStatus
    accepted: bool
    reason_codes: tuple[str, ...]
    current_bi_idx: int | None = None
    previous_bi_idx: int | None = None
    area_current: float | None = None
    area_previous: float | None = None
    area_ratio: float | None = None
    peak_current: float | None = None
    peak_previous: float | None = None
    peak_ratio: float | None = None
    price_strength_current: float | None = None
    price_strength_previous: float | None = None
    price_strength_ratio: float | None = None
    slope_current: float | None = None
    slope_previous: float | None = None
    slope_ratio: float | None = None
    effective_extension: float | None = None
    histogram_state: str = "unavailable"

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "status": self.status.value,
            "accepted": self.accepted,
            "reason_codes": self.reason_codes,
            "current_bi_idx": self.current_bi_idx,
            "previous_bi_idx": self.previous_bi_idx,
            "area_current": self.area_current,
            "area_previous": self.area_previous,
            "area_ratio": self.area_ratio,
            "peak_current": self.peak_current,
            "peak_previous": self.peak_previous,
            "peak_ratio": self.peak_ratio,
            "price_strength_current": self.price_strength_current,
            "price_strength_previous": self.price_strength_previous,
            "price_strength_ratio": self.price_strength_ratio,
            "slope_current": self.slope_current,
            "slope_previous": self.slope_previous,
            "slope_ratio": self.slope_ratio,
            "effective_extension": self.effective_extension,
            "histogram_state": self.histogram_state,
        }


class QingpaiMomentumAnalyzer:
    """Build and enforce one auditable composite momentum confirmation."""

    def __init__(self, params) -> None:
        self.params = params

    def apply(
        self,
        intent,
        *,
        chan,
        decision_time: object,
        lv_idx: int = 0,
    ):
        confirmation = self.analyze_intent(
            intent,
            chan=chan,
            decision_time=decision_time,
            lv_idx=lv_idx,
        )
        updated = replace(intent, momentum=confirmation)
        if not intent.accepted or confirmation.accepted:
            return updated
        reasons = tuple(
            dict.fromkeys((*intent.decision.reason_codes, *confirmation.reason_codes))
        )
        return replace(
            updated,
            decision=replace(
                intent.decision,
                accepted=False,
                reason_codes=reasons,
                entry_price_hint=None,
                position_size_hint=None,
            ),
        )

    def analyze_intent(
        self,
        intent,
        *,
        chan,
        decision_time: object,
        lv_idx: int = 0,
    ) -> MomentumConfirmation:
        bsp_types = {
            part.strip() for part in intent.signal.bsp_type.split(",") if part.strip()
        }
        applies = bool(bsp_types.intersection({"1", "1p"}))
        if not applies:
            return self._not_applicable(decision_time)
        bi_idx = intent.event.bi_idx if intent.event is not None else intent.signal.bsp_bi_idx
        return self.analyze_bi(
            chan,
            bi_idx=int(bi_idx),
            observed_at=decision_time,
            lv_idx=lv_idx,
            required=self.params.require_t1_confirmation,
        )

    def analyze_latest_direction(
        self,
        chan,
        *,
        direction: str,
        observed_at: object,
        lv_idx: int = 0,
    ) -> MomentumConfirmation:
        try:
            bi_list = chan[lv_idx].bi_list
            candidates = [
                bi for bi in bi_list
                if (direction == "long" and bi.is_up())
                or (direction == "short" and bi.is_down())
            ]
        except Exception:
            candidates = []
        if not candidates:
            return self._unavailable(observed_at, "momentum_current_bi_unavailable")
        return self.analyze_bi(
            chan,
            bi_idx=int(candidates[-1].idx),
            observed_at=observed_at,
            lv_idx=lv_idx,
            required=False,
        )

    def analyze_bi(
        self,
        chan,
        *,
        bi_idx: int,
        observed_at: object,
        lv_idx: int = 0,
        required: bool = True,
    ) -> MomentumConfirmation:
        timestamp = market_timestamp(observed_at)
        try:
            bi_list = chan[lv_idx].bi_list
            current = bi_list[bi_idx]
        except Exception:
            return self._unavailable(timestamp, "momentum_current_bi_unavailable")

        previous = None
        for candidate in reversed(list(bi_list)[:bi_idx]):
            if candidate.dir == current.dir:
                previous = candidate
                break
        if previous is None:
            return self._unavailable(timestamp, "momentum_previous_bi_unavailable")

        try:
            area_current = float(
                current.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False)
            )
            area_previous = float(
                previous.cal_macd_metric(MACD_ALGO.FULL_AREA, is_reverse=False)
            )
            peak_current = float(current.cal_macd_metric(MACD_ALGO.PEAK, False))
            peak_previous = float(previous.cal_macd_metric(MACD_ALGO.PEAK, False))
            price_current = abs(float(current.get_end_val() - current.get_begin_val()))
            price_previous = abs(float(previous.get_end_val() - previous.get_begin_val()))
            slope_current = price_current / max(1, int(current.get_klu_cnt()))
            slope_previous = price_previous / max(1, int(previous.get_klu_cnt()))
            extension = _effective_extension(previous, current)
        except Exception:
            return self._unavailable(timestamp, "momentum_metric_unavailable")

        values = (
            area_current,
            area_previous,
            peak_current,
            peak_previous,
            price_current,
            price_previous,
            slope_current,
            slope_previous,
        )
        if any(not isfinite(value) for value in values) or min(
            area_previous, peak_previous, price_previous, slope_previous
        ) <= 0:
            return self._unavailable(timestamp, "momentum_metric_invalid")

        area_ratio = area_current / area_previous
        peak_ratio = peak_current / peak_previous
        price_ratio = price_current / price_previous
        slope_ratio = slope_current / slope_previous
        histogram_state = _histogram_lifecycle(
            current,
            shrink_ratio=self.params.histogram_shrink_ratio,
        )

        area_weaker = area_ratio <= self.params.area_ratio_max
        area_near = area_ratio <= self.params.area_near_ratio
        peak_weaker = peak_ratio <= self.params.peak_ratio_max
        price_weaker = (
            price_ratio <= self.params.price_strength_ratio_max
            or slope_ratio <= self.params.price_strength_ratio_max
        )
        micro_extension = (
            extension <= price_previous * self.params.micro_extension_ratio
        )
        composite = (
            area_weaker and (price_weaker or micro_extension)
        ) or (
            area_near and peak_weaker and price_weaker
        )

        reasons: list[str] = []
        if not area_weaker and not (area_near and peak_weaker):
            reasons.append("macd_area_peak_not_weaker")
        if not price_weaker and not micro_extension:
            reasons.append("price_force_not_weaker")
        if (
            self.params.require_histogram_confirmation
            and histogram_state != MomentumStatus.CONFIRMED.value
        ):
            reasons.append("macd_histogram_not_confirmed")

        histogram_ok = (
            not self.params.require_histogram_confirmation
            or histogram_state == MomentumStatus.CONFIRMED.value
        )
        confirmed = composite and histogram_ok
        if confirmed:
            status = MomentumStatus.CONFIRMED
        elif composite or histogram_state == MomentumStatus.CANDIDATE.value:
            status = MomentumStatus.CANDIDATE
        else:
            status = MomentumStatus.REJECTED
        accepted = confirmed or not required
        return MomentumConfirmation(
            observed_at=timestamp,
            available_at=timestamp,
            status=status,
            accepted=accepted,
            reason_codes=tuple(dict.fromkeys(reasons)),
            current_bi_idx=int(current.idx),
            previous_bi_idx=int(previous.idx),
            area_current=area_current,
            area_previous=area_previous,
            area_ratio=area_ratio,
            peak_current=peak_current,
            peak_previous=peak_previous,
            peak_ratio=peak_ratio,
            price_strength_current=price_current,
            price_strength_previous=price_previous,
            price_strength_ratio=price_ratio,
            slope_current=slope_current,
            slope_previous=slope_previous,
            slope_ratio=slope_ratio,
            effective_extension=extension,
            histogram_state=histogram_state,
        )

    @staticmethod
    def _not_applicable(observed_at: object) -> MomentumConfirmation:
        timestamp = market_timestamp(observed_at)
        return MomentumConfirmation(
            observed_at=timestamp,
            available_at=timestamp,
            status=MomentumStatus.NOT_APPLICABLE,
            accepted=True,
            reason_codes=(),
        )

    @staticmethod
    def _unavailable(observed_at: object, reason: str) -> MomentumConfirmation:
        timestamp = market_timestamp(observed_at)
        return MomentumConfirmation(
            observed_at=timestamp,
            available_at=timestamp,
            status=MomentumStatus.UNAVAILABLE,
            accepted=False,
            reason_codes=(reason,),
        )


def _effective_extension(previous, current) -> float:
    previous_end = float(previous.get_end_val())
    current_end = float(current.get_end_val())
    if current.is_up():
        return max(0.0, current_end - previous_end)
    return max(0.0, previous_end - current_end)


def _histogram_lifecycle(bi, *, shrink_ratio: float) -> str:
    try:
        end = bi.get_end_klu()
        previous = end.pre
        before_previous = previous.pre if previous is not None else None
        if previous is None:
            return MomentumStatus.UNAVAILABLE.value
        current_value = float(end.macd.macd)
        previous_value = float(previous.macd.macd)
        color_switched = current_value * previous_value <= 0
        first_shrink = abs(current_value) <= abs(previous_value) * shrink_ratio
        second_shrink = False
        if before_previous is not None:
            prior_value = float(before_previous.macd.macd)
            second_shrink = (
                abs(previous_value) <= abs(prior_value) * shrink_ratio
            )
        if color_switched or (first_shrink and second_shrink):
            return MomentumStatus.CONFIRMED.value
        if first_shrink:
            return MomentumStatus.CANDIDATE.value
        return MomentumStatus.REJECTED.value
    except Exception:
        return MomentumStatus.UNAVAILABLE.value
