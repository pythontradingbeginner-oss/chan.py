"""Point-in-time chan structure snapshot shared by all execution runtimes."""

from __future__ import annotations

from strategy_policy.exit_rules import ChanExitSnapshot

from .multi_level import market_timestamp
from .qingpai_momentum import QingpaiMomentumAnalyzer


class ChanExitSnapshotBuilder:
    """Translate mutable CChan structures into one immutable exit snapshot."""

    def __init__(self, momentum: QingpaiMomentumAnalyzer | None = None) -> None:
        self.momentum = momentum
        self._seen_sure_segments: set[str] = set()

    def reset(self) -> None:
        self._seen_sure_segments.clear()

    def build(
        self,
        chan,
        *,
        observed_at: object,
        direction: str,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        average_amplitude: float | None = None,
        price_adjustment: float = 0.0,
        lv_idx: int = 0,
    ) -> ChanExitSnapshot:
        timestamp = market_timestamp(observed_at)
        bi_list = _safe_list(chan, lv_idx, "bi_list")
        seg_list = _safe_list(chan, lv_idx, "seg_list")
        sure_segments = [seg for seg in seg_list if bool(getattr(seg, "is_sure", False))]
        latest_segment = sure_segments[-1] if sure_segments else None
        segment_id = _segment_id(latest_segment)
        segment_complete = bool(
            segment_id and segment_id not in self._seen_sure_segments
        )
        if segment_id:
            self._seen_sure_segments.add(segment_id)

        momentum = None
        if self.momentum is not None:
            momentum = self.momentum.analyze_latest_direction(
                chan,
                direction=direction,
                observed_at=timestamp,
                lv_idx=lv_idx,
            )

        sign = 1.0 if direction == "long" else -1.0
        areas = None
        peaks = None
        strengths = None
        slopes = None
        if momentum is not None and momentum.area_previous is not None:
            areas = [
                sign * momentum.area_previous,
                sign * float(momentum.area_current),
            ]
            peaks = [float(momentum.peak_previous), float(momentum.peak_current)]
            strengths = [
                float(momentum.price_strength_previous),
                float(momentum.price_strength_current),
            ]
            slopes = [float(momentum.slope_previous), float(momentum.slope_current)]

        signed_amplitudes = [
            (1.0 if bi.is_up() else -1.0) * float(bi.amp())
            for bi in bi_list[-8:]
        ]
        current_same = [
            bi
            for bi in bi_list
            if (direction == "long" and bi.is_up())
            or (direction == "short" and bi.is_down())
        ]
        is_new_extreme = False
        prev_high = None
        prev_low = None
        if len(current_same) >= 2:
            previous_value = _raw_price(
                current_same[-2].get_end_val(), price_adjustment
            )
            current_value = _raw_price(
                current_same[-1].get_end_val(), price_adjustment
            )
            if direction == "long":
                prev_high = previous_value
                is_new_extreme = current_value > previous_value
            else:
                prev_low = previous_value
                is_new_extreme = current_value < previous_value

        end_bi_idx = (
            int(latest_segment.end_bi.idx) if latest_segment is not None else -1
        )
        new_bis = [
            bi
            for bi in bi_list
            if int(bi.idx) > end_bi_idx and bool(getattr(bi, "is_sure", False))
        ]
        latest_new_bi = new_bis[-1] if new_bis else None
        fractal_level = _fractal_break_level(
            bi_list,
            direction=direction,
            adjustment=price_adjustment,
        )
        fractal_broken = False
        if fractal_level is not None:
            fractal_broken = (
                float(low_price) < fractal_level
                if direction == "long"
                else float(high_price) > fractal_level
            )

        return ChanExitSnapshot(
            available_at=timestamp,
            segment_complete=segment_complete,
            segment_direction=(
                ("up" if latest_segment.is_up() else "down")
                if latest_segment is not None else None
            ),
            segment_id=segment_id,
            macd_areas=areas,
            macd_area_current=(momentum.area_current if momentum else None),
            macd_area_previous=(momentum.area_previous if momentum else None),
            macd_area_ratio=(momentum.area_ratio if momentum else None),
            macd_peaks=peaks,
            price_strengths=strengths,
            price_slopes=slopes,
            momentum_status=(momentum.status.value if momentum else None),
            momentum_confirmed=(
                momentum is not None and momentum.status.value == "confirmed"
            ),
            momentum_reason_codes=(momentum.reason_codes if momentum else ()),
            histogram_state=(momentum.histogram_state if momentum else None),
            adjacent_bidong=signed_amplitudes or None,
            new_bi_confirmed=latest_new_bi is not None,
            new_bi_amplitude=(
                float(latest_new_bi.amp()) if latest_new_bi is not None else None
            ),
            is_new_high_low=is_new_extreme,
            prev_high=prev_high,
            prev_low=prev_low,
            fractal_break_price=fractal_level,
            fractal_break_confirmed=fractal_broken,
            bar_amplitude=abs(float(high_price) - float(low_price)),
            avg_amplitude_20=average_amplitude,
            structure_version="qingpai_p6_v1",
        )


def _safe_list(chan, lv_idx: int, name: str) -> list:
    try:
        return list(getattr(chan[lv_idx], name))
    except Exception:
        return []


def _segment_id(segment) -> str | None:
    if segment is None:
        return None
    return f"seg:{int(segment.idx)}:{int(segment.end_bi.idx)}"


def _raw_price(value: float, adjustment: float) -> float:
    return float(value) - float(adjustment)


def _fractal_break_level(
    bi_list: list,
    *,
    direction: str,
    adjustment: float,
) -> float | None:
    for bi in reversed(bi_list):
        if direction == "long" and bi.is_down():
            return _raw_price(bi.get_end_val(), adjustment)
        if direction == "short" and bi.is_up():
            return _raw_price(bi.get_end_val(), adjustment)
    return None
