"""Grade-filtered wrapper around MinimalChanTrendStrategy.

Wraps the original strategy and adds a grade filter:
  - signal → extract SignalEvent → assess → only trade if grade >= min_grade

This keeps the original strategy unchanged and adds the grade pipeline
as a transparent layer between strategy.on_bar and risk/execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from Common.CEnum import BSP_TYPE, KL_TYPE

from .strategy import MinimalChanTrendStrategy, StrategySignal


# ── grade filter configuration ────────────────────────

@dataclass(frozen=True)
class GradeFilterConfig:
    """Which BSP types & grades to allow through."""
    accepted_bsp_types: Iterable[BSP_TYPE] | None = None
    allow_short: bool = True
    require_confirmed_bsp: bool = True
    min_grade: str = "standard"  # "ideal" | "standard" | "weak"


# ── graded signal (extends StrategySignal with assessment info) ─

@dataclass(frozen=True)
class GradedSignal:
    """StrategySignal + assessment snapshot for audit/logging."""
    signal: StrategySignal
    bsp_type: str
    grade: str
    structural_score: float
    event_id: str
    signal_key: str


# ── wrapper class ─────────────────────────────────────

class GradedChanStrategy:
    """Wraps MinimalChanTrendStrategy + adds grade filtering.

    Usage:
        strategy = GradedChanStrategy(GradeFilterConfig(min_grade="standard"))
        graded = strategy.on_bar(chan=chan, current_position=0, price=price,
                                  timestamp=ts, extractor=extractor, lv_idx=0)
        if graded is not None:
            risk.approve(graded.signal, ...)
            execution.execute(graded.signal)
    """

    def __init__(self, config: GradeFilterConfig | None = None) -> None:
        cfg = config or GradeFilterConfig()
        self._inner = MinimalChanTrendStrategy(
            accepted_bsp_types=cfg.accepted_bsp_types,
            allow_short=cfg.allow_short,
            require_confirmed_bsp=cfg.require_confirmed_bsp,
        )
        self.min_grade = cfg.min_grade
        # 用于生成 signal_id: (bi_idx, dir, primary_bsp) → revision
        self._signal_seq: dict[tuple[int, str, str], int] = {}

    def on_bar(
        self,
        *,
        chan,
        current_position: int,
        price: float,
        timestamp: object,
        active_symbol: str | None = None,
        lv_idx: int = 0,
        extractor=None,   # SignalExtractor — required for grading
    ) -> GradedSignal | None:
        """Generate a signal and grade it. Returns None if below min_grade."""
        inner_sig = self._inner.on_bar(
            chan=chan,
            current_position=current_position,
            price=price,
            timestamp=timestamp,
            active_symbol=active_symbol,
            lv_idx=lv_idx,
        )
        if inner_sig is None:
            return None
        if extractor is None:
            # No grader → pass through with default grade info
            return GradedSignal(
                signal=inner_sig,
                bsp_type=inner_sig.bsp_type,
                grade="standard",
                structural_score=0.0,
                event_id="",
                signal_key="",
            )

        return self._grade(inner_sig, chan, extractor, timestamp, lv_idx)

    def _grade(
        self,
        sig: StrategySignal,
        chan,
        extractor,
        timestamp,
        lv_idx: int,
    ) -> GradedSignal | None:
        """Extract + assess the BSP that backs this StrategySignal, filter by grade."""
        try:
            bsp_list = chan[lv_idx].bs_point_lst
        except Exception:
            return None

        # Find the CBS_Point matching this StrategySignal's bi_idx + bsp_type
        matched_bsp = None
        for bsp in bsp_list.bsp_iter():
            if bsp.bi.idx == sig.bsp_bi_idx and sig.bsp_type in bsp.type2str():
                matched_bsp = bsp
                break

        if matched_bsp is None:
            return GradedSignal(
                signal=sig, bsp_type=sig.bsp_type,
                grade="standard", structural_score=0.0,
                event_id="", signal_key="",
            )

        # Extract + assess
        from signal_scoring import assess_event

        # resolve bar_end_time from timestamp
        bar_end_time = timestamp
        if isinstance(timestamp, datetime):
            bar_end_time = timestamp
        elif hasattr(timestamp, "to_pydatetime"):
            bar_end_time = timestamp.to_pydatetime()

        event = extractor.extract(matched_bsp, chan=chan, bar_end_time=bar_end_time, lv_idx=lv_idx)
        if event is None:
            return None

        assessment = assess_event(event)

        # Grade filter: reject below min_grade
        grade_order = {"ideal": 3, "standard": 2, "weak": 1}
        if grade_order.get(assessment.grade.value, 0) < grade_order.get(self.min_grade, 0):
            return None

        return GradedSignal(
            signal=sig,
            bsp_type=sig.bsp_type,
            grade=assessment.grade.value,
            structural_score=assessment.structural_score or 0.0,
            event_id=event.event_id,
            signal_key=event.signal_key,
        )
