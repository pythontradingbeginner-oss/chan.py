"""Shared runtime decision entry used by backtest, CTA and live engines."""

from __future__ import annotations

from dataclasses import replace

from signal_core import SignalExtractor
from strategy_policy.qingpai_decomposition import (
    DecompositionSnapshot,
    DecompositionTransition,
    QingpaiDecomposer,
)

from .graded_strategy import GradedChanStrategy
from .exit_snapshot import ChanExitSnapshotBuilder
from .multi_level import (
    MultiLevelDecisionContext,
    MultiLevelDecisionEngine,
    MultiLevelReadiness,
)
from .qingpai_momentum import QingpaiMomentumAnalyzer
from .trade_intent import DecisionTraceRecord, TradeIntent


class RuntimeDecisionKernel:
    """Evaluate bars and record one canonical decision trace."""

    def __init__(
        self,
        strategy: GradedChanStrategy,
        extractor: SignalExtractor,
        decomposer: QingpaiDecomposer | None = None,
        multi_level: MultiLevelDecisionEngine | None = None,
        momentum: QingpaiMomentumAnalyzer | None = None,
    ) -> None:
        self.strategy = strategy
        self.extractor = extractor
        self.decomposer = decomposer
        self.multi_level = multi_level
        self.momentum = momentum
        self.exit_snapshot_builder = ChanExitSnapshotBuilder(momentum)
        self._decision_trace: list[DecisionTraceRecord] = []
        self._decomposition_transition_archive: list[DecompositionTransition] = []

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
        available_funds: float | None = None,
        atr: float | None = None,
        price_adjustment: float = 0.0,
        risk_session_key: str | None = None,
        risk_session_observed_at: object | None = None,
        risk_session_source: str = "",
        equity_source: str = "",
        equity_scope: str = "",
        max_drawdown_capability: str = "",
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
            available_funds=available_funds,
            atr=atr,
            price_adjustment=price_adjustment,
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
            if self.momentum is not None:
                was_accepted = intent.accepted
                intent = self.momentum.apply(
                    intent,
                    chan=chan,
                    decision_time=timestamp,
                    lv_idx=lv_idx,
                )
                if was_accepted and not intent.accepted:
                    self.strategy.release_signal(intent.signal)
            self._decision_trace.append(
                intent.to_trace_record(
                    risk_session_key=risk_session_key,
                    risk_session_observed_at=risk_session_observed_at,
                    risk_session_source=risk_session_source,
                    equity_value=account_equity,
                    equity_source=equity_source,
                    equity_scope=equity_scope,
                    max_drawdown_capability=max_drawdown_capability,
                )
            )
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
        current = self.decomposer.transitions if self.decomposer is not None else ()
        combined = (*self._decomposition_transition_archive, *current)
        return tuple(
            replace(transition, sequence=index)
            for index, transition in enumerate(combined)
        )

    @property
    def multi_level_audit(self) -> tuple[MultiLevelDecisionContext, ...]:
        return self.multi_level.audit if self.multi_level is not None else ()

    @property
    def multi_level_readiness(self) -> MultiLevelReadiness | None:
        return self.multi_level.readiness if self.multi_level is not None else None

    def reset_contract_state(
        self,
        *,
        start_time: object | None = None,
        active_symbol: str | None = None,
    ) -> None:
        """Clear identities that cannot cross an actual-contract rollover."""
        self.strategy.reset()
        reset = getattr(self.extractor, "reset", None)
        if callable(reset):
            reset()
        self.exit_snapshot_builder.reset()
        if self.decomposer is not None:
            self._decomposition_transition_archive.extend(
                self.decomposer.transitions
            )
            self.decomposer = QingpaiDecomposer(level=self.decomposer.level)
        if self.multi_level is not None:
            self.multi_level.reset_contract_state(
                start_time=start_time,
                active_symbol=active_symbol,
            )

    def build_exit_snapshot(self, chan, **kwargs):
        return self.exit_snapshot_builder.build(chan, **kwargs)
