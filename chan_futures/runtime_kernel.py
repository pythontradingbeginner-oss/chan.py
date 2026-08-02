"""Shared runtime decision entry used by backtest, CTA and live engines."""

from __future__ import annotations

from signal_core import SignalExtractor
from strategy_policy.qingpai_decomposition import (
    DecompositionSnapshot,
    DecompositionTransition,
    QingpaiDecomposer,
)

from .graded_strategy import GradedChanStrategy
from .multi_level import MultiLevelDecisionContext, MultiLevelDecisionEngine
from .trade_intent import DecisionTraceRecord, TradeIntent


class RuntimeDecisionKernel:
    """Evaluate bars and record one canonical decision trace."""

    def __init__(
        self,
        strategy: GradedChanStrategy,
        extractor: SignalExtractor,
        decomposer: QingpaiDecomposer | None = None,
        multi_level: MultiLevelDecisionEngine | None = None,
    ) -> None:
        self.strategy = strategy
        self.extractor = extractor
        self.decomposer = decomposer
        self.multi_level = multi_level
        self._decision_trace: list[DecisionTraceRecord] = []

    def observe_structure(
        self,
        *,
        chan,
        timestamp: object,
        lv_idx: int = 0,
    ) -> DecompositionSnapshot | None:
        if self.decomposer is None:
            return None
        return self.decomposer.update_from_chan(
            chan,
            observed_at=timestamp,
            lv_idx=lv_idx,
        )

    def evaluate_bar(
        self,
        *,
        chan,
        current_position: int,
        price: float,
        timestamp: object,
        active_symbol: str | None = None,
        lv_idx: int = 0,
        account_equity: float | None = None,
        atr: float | None = None,
    ) -> TradeIntent | None:
        if self.multi_level is not None:
            self.multi_level.advance_to(timestamp)
        decomposition = self.observe_structure(
            chan=chan,
            timestamp=timestamp,
            lv_idx=lv_idx,
        )
        intent = self.strategy.evaluate_bar(
            chan=chan,
            current_position=current_position,
            price=price,
            timestamp=timestamp,
            active_symbol=active_symbol,
            lv_idx=lv_idx,
            extractor=self.extractor,
            account_equity=account_equity,
            atr=atr,
        )
        if intent is not None:
            intent = intent.with_decomposition(decomposition)
            if self.multi_level is not None:
                intent = self.multi_level.apply(
                    intent,
                    current_chan=chan,
                    decision_time=timestamp,
                    lv_idx=lv_idx,
                )
            self._decision_trace.append(intent.to_trace_record())
        return intent

    def observe_parent_bar(self, klu, *, available_at: object | None = None) -> None:
        if self.multi_level is not None:
            self.multi_level.observe_parent_bar(klu, available_at=available_at)

    def observe_child_bar(self, klu, *, available_at: object | None = None) -> None:
        if self.multi_level is not None:
            self.multi_level.observe_child_bar(klu, available_at=available_at)

    @property
    def decision_trace(self) -> tuple[DecisionTraceRecord, ...]:
        return tuple(self._decision_trace)

    @property
    def decomposition_state(self) -> DecompositionSnapshot | None:
        return self.decomposer.current if self.decomposer is not None else None

    @property
    def decomposition_transitions(self) -> tuple[DecompositionTransition, ...]:
        return self.decomposer.transitions if self.decomposer is not None else ()

    @property
    def multi_level_audit(self) -> tuple[MultiLevelDecisionContext, ...]:
        return self.multi_level.audit if self.multi_level is not None else ()
