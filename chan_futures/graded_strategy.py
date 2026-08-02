"""Grade-filtered wrapper around MinimalChanTrendStrategy.

Wraps the original strategy and adds a grade filter:
  - signal → extract SignalEvent → assess → only trade if grade >= min_grade

This keeps the original strategy unchanged and adds the grade pipeline
as a transparent layer between strategy.on_bar and risk/execution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable

from Common.CEnum import BSP_TYPE
from signal_core.models import SignalAssessment, SignalDecision, SignalEvent
from signal_scoring import assess_event

from .decision_pipeline import DecisionMode, DecisionPipeline, DecisionPipelineConfig
from .strategy import MinimalChanTrendStrategy, StrategySignal


# ── grade filter configuration ────────────────────────

@dataclass(frozen=True)
class GradeFilterConfig:
    """Which BSP types & grades to allow through."""
    accepted_bsp_types: Iterable[BSP_TYPE] | None = None
    allow_short: bool = True
    require_confirmed_bsp: bool = True
    min_grade: str = "standard"  # "ideal" | "standard" | "weak"
    policy_mode: str = DecisionMode.LEGACY.value
    policy_id: str = "chan_entry_v1"


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
    decision: SignalDecision | None = None
    event: SignalEvent | None = None
    assessment: SignalAssessment | None = None

    @property
    def accepted(self) -> bool:
        return self.decision.accepted if self.decision is not None else True


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

    def __init__(
        self,
        config: GradeFilterConfig | None = None,
        *,
        decision_pipeline: DecisionPipeline | None = None,
    ) -> None:
        cfg = config or GradeFilterConfig()
        accepted = tuple(cfg.accepted_bsp_types) if cfg.accepted_bsp_types is not None else None
        self._inner = MinimalChanTrendStrategy(
            accepted_bsp_types=accepted,
            allow_short=cfg.allow_short,
            require_confirmed_bsp=cfg.require_confirmed_bsp,
        )
        self.min_grade = cfg.min_grade
        accepted_values = frozenset(
            bsp_type.value for bsp_type in self._inner.accepted_bsp_types
        )
        self._decision_pipeline = decision_pipeline or DecisionPipeline(
            DecisionPipelineConfig(
                policy_id=cfg.policy_id,
                mode=DecisionMode(cfg.policy_mode),
                min_grade=cfg.min_grade,
                accepted_bsp_types=accepted_values,
                allow_short=cfg.allow_short,
                require_confirmed=cfg.require_confirmed_bsp,
            )
        )
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
        account_equity: float | None = None,
        atr: float | None = None,
    ) -> GradedSignal | None:
        """Preserve the old API: return only accepted decisions."""
        evaluated = self.evaluate_bar(
            chan=chan,
            current_position=current_position,
            price=price,
            timestamp=timestamp,
            active_symbol=active_symbol,
            lv_idx=lv_idx,
            extractor=extractor,
            account_equity=account_equity,
            atr=atr,
        )
        if evaluated is None or not evaluated.accepted:
            return None
        return evaluated

    def evaluate_bar(
        self,
        *,
        chan,
        current_position: int,
        price: float,
        timestamp: object,
        active_symbol: str | None = None,
        lv_idx: int = 0,
        extractor=None,
        account_equity: float | None = None,
        atr: float | None = None,
    ) -> GradedSignal | None:
        """Return the full accepted/rejected decision for runtime auditing."""
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
            return self._build_result(
                inner_sig, None, None, account_equity=account_equity, atr=atr
            )

        return self._grade(
            inner_sig,
            chan,
            extractor,
            timestamp,
            lv_idx,
            account_equity=account_equity,
            atr=atr,
        )

    def _grade(
        self,
        sig: StrategySignal,
        chan,
        extractor,
        timestamp,
        lv_idx: int,
        *,
        account_equity: float | None = None,
        atr: float | None = None,
    ) -> GradedSignal:
        """Extract and assess the exact BSP backing the strategy signal."""
        try:
            bsp_list = chan[lv_idx].bs_point_lst
        except Exception:
            return self._build_result(
                sig, None, None, account_equity=account_equity, atr=atr
            )

        # Match the exact source identity; substring matching confuses e.g. 1 and 1p.
        matched_bsp = None
        for bsp in bsp_list.bsp_iter():
            if (
                bsp.bi.idx == sig.bsp_bi_idx
                and bsp.klu.idx == sig.bsp_klu_idx
                and bsp.type2str() == sig.bsp_type
            ):
                matched_bsp = bsp
                break

        if matched_bsp is None:
            return self._build_result(
                sig, None, None, account_equity=account_equity, atr=atr
            )

        # resolve bar_end_time from timestamp
        bar_end_time = timestamp
        if isinstance(timestamp, datetime):
            bar_end_time = timestamp
        elif hasattr(timestamp, "to_pydatetime"):
            bar_end_time = timestamp.to_pydatetime()

        event = extractor.extract(matched_bsp, chan=chan, bar_end_time=bar_end_time, lv_idx=lv_idx)
        if event is None:
            get_current = getattr(extractor, "get_current", None)
            if callable(get_current):
                event = get_current(matched_bsp)
        if event is None:
            return self._build_result(
                sig, None, None, account_equity=account_equity, atr=atr
            )

        assessment = assess_event(event)
        return self._build_result(
            sig, event, assessment, account_equity=account_equity, atr=atr
        )

    def _build_result(
        self,
        sig: StrategySignal,
        event: SignalEvent | None,
        assessment: SignalAssessment | None,
        *,
        account_equity: float | None = None,
        atr: float | None = None,
    ) -> GradedSignal:
        decision = self._decision_pipeline.evaluate(
            sig,
            event,
            assessment,
            account_equity=account_equity,
            atr=atr,
        )
        if not decision.accepted and "signal_not_confirmed" in decision.reason_codes:
            self._inner.release_signal(sig)
        executable_signal = sig
        if decision.accepted and sig.target_position != 0:
            lots = int(decision.position_size_hint or 0)
            signed_target = lots if sig.target_position > 0 else -lots
            executable_signal = replace(sig, target_position=signed_target)
        return GradedSignal(
            signal=executable_signal,
            bsp_type=sig.bsp_type,
            grade=assessment.grade.value if assessment is not None else "standard",
            structural_score=(assessment.structural_score or 0.0) if assessment is not None else 0.0,
            event_id=event.event_id if event is not None else "",
            signal_key=event.signal_key if event is not None else "",
            decision=decision,
            event=event,
            assessment=assessment,
        )
