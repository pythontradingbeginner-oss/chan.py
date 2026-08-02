"""Shared runtime decision entry used by backtest, CTA and live engines."""

from __future__ import annotations

from signal_core import SignalExtractor

from .graded_strategy import GradedChanStrategy
from .trade_intent import DecisionTraceRecord, TradeIntent


class RuntimeDecisionKernel:
    """Evaluate bars and record one canonical decision trace."""

    def __init__(
        self,
        strategy: GradedChanStrategy,
        extractor: SignalExtractor,
    ) -> None:
        self.strategy = strategy
        self.extractor = extractor
        self._decision_trace: list[DecisionTraceRecord] = []

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
            self._decision_trace.append(intent.to_trace_record())
        return intent

    @property
    def decision_trace(self) -> tuple[DecisionTraceRecord, ...]:
        return tuple(self._decision_trace)

