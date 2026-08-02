from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from Common.CEnum import MACD_ALGO
from chan_futures.backtest import run_backtest
from chan_futures.config import (
    ExecutionParams,
    MomentumParams,
    ProductionParams,
    RiskParams,
    SizingParams,
    StrategyConfig,
)
from chan_futures.decision_pipeline import DecisionPipeline, DecisionPipelineConfig
from chan_futures.execution import SimulatedExecutionEngine, adverse_fill_price
from chan_futures.qingpai_momentum import (
    MomentumStatus,
    QingpaiMomentumAnalyzer,
)
from chan_futures.production import _replay_start_index
from chan_futures.strategy import StrategySignal
from chan_futures.trade_intent import TradeIntent
from data_foundation import aggregate_continuous_1m_to_5m
from signal_core.models import SignalDecision, SignalDirection
from strategy_policy.exit_rules import (
    ChanDivergenceExitRule,
    ChanExitSnapshot,
    ExitManager,
    OppositeSignalRule,
    StructureInvalidationExitRule,
    StructureStopRule,
)


NOW = datetime(2025, 1, 2, 10, 0)


class _FixedSizer:
    def __init__(self, lots: int) -> None:
        self.lots = lots

    def calculate(self, **kwargs) -> int:
        return self.lots


class _FakeKlu:
    def __init__(self, macd: float, pre=None) -> None:
        self.macd = SimpleNamespace(macd=macd)
        self.pre = pre


class _FakeBi:
    def __init__(
        self,
        idx: int,
        direction: str,
        *,
        area: float,
        peak: float,
        begin: float,
        end: float,
        bars: int,
        histogram: tuple[float, float, float] = (4.0, 3.0, 2.0),
    ) -> None:
        self.idx = idx
        self.dir = direction
        self._area = area
        self._peak = peak
        self._begin = begin
        self._end = end
        self._bars = bars
        first = _FakeKlu(histogram[0])
        second = _FakeKlu(histogram[1], first)
        self._end_klu = _FakeKlu(histogram[2], second)

    def cal_macd_metric(self, algo, is_reverse=False):
        return self._area if algo == MACD_ALGO.FULL_AREA else self._peak

    def get_begin_val(self):
        return self._begin

    def get_end_val(self):
        return self._end

    def get_klu_cnt(self):
        return self._bars

    def get_end_klu(self):
        return self._end_klu

    def is_up(self):
        return self.dir == "up"

    def is_down(self):
        return self.dir == "down"


class _FakeChan:
    def __init__(self, bis: list[_FakeBi]) -> None:
        self.level = SimpleNamespace(bi_list=bis)

    def __getitem__(self, index: int):
        return self.level


def test_momentum_area_is_primary_and_price_force_is_final_gate() -> None:
    analyzer = QingpaiMomentumAnalyzer(MomentumParams(enabled=True))
    previous = _FakeBi(
        0, "up", area=100, peak=20, begin=100, end=200, bars=10
    )
    opposite = _FakeBi(
        1, "down", area=70, peak=15, begin=200, end=180, bars=5
    )
    current = _FakeBi(
        2, "up", area=80, peak=18, begin=180, end=270, bars=10
    )
    confirmed = analyzer.analyze_bi(
        _FakeChan([previous, opposite, current]),
        bi_idx=2,
        observed_at=NOW,
    )
    assert confirmed.status == MomentumStatus.CONFIRMED
    assert confirmed.accepted
    assert confirmed.area_ratio == 0.8
    assert confirmed.price_strength_ratio == 0.9

    stronger_price = _FakeBi(
        2, "up", area=80, peak=18, begin=180, end=320, bars=10
    )
    rejected = analyzer.analyze_bi(
        _FakeChan([previous, opposite, stronger_price]),
        bi_idx=2,
        observed_at=NOW,
    )
    assert rejected.status == MomentumStatus.REJECTED
    assert "price_force_not_weaker" in rejected.reason_codes


def test_momentum_peak_resolves_area_near_tie_and_histogram_needs_confirmation() -> None:
    analyzer = QingpaiMomentumAnalyzer(MomentumParams(enabled=True))
    previous = _FakeBi(
        0, "up", area=100, peak=20, begin=100, end=200, bars=10
    )
    opposite = _FakeBi(
        1, "down", area=50, peak=10, begin=200, end=180, bars=5
    )
    current = _FakeBi(
        2,
        "up",
        area=102,
        peak=16,
        begin=180,
        end=270,
        bars=10,
        histogram=(2.0, 4.0, 3.0),
    )
    candidate = analyzer.analyze_bi(
        _FakeChan([previous, opposite, current]),
        bi_idx=2,
        observed_at=NOW,
    )
    assert candidate.peak_ratio == 0.8
    assert candidate.status == MomentumStatus.CANDIDATE
    assert not candidate.accepted
    assert candidate.histogram_state == MomentumStatus.CANDIDATE.value


def test_margin_cap_is_applied_after_risk_sizing() -> None:
    pipeline = DecisionPipeline(
        DecisionPipelineConfig(
            contract_multiplier=10,
            margin_rate=0.10,
            max_margin_utilization=0.50,
        ),
        sizer=_FixedSizer(10),
    )
    decision = _decision(entry=1000, stop=990)
    signal = _signal(price=1000, target=1)
    capped = pipeline._size_decision(
        decision,
        signal=signal,
        account_equity=100_000,
        available_funds=5_000,
        atr=None,
    )
    assert capped.accepted
    assert capped.position_size_hint == 2
    assert "margin_cap_applied" in capped.reason_codes

    blocked = pipeline._size_decision(
        decision,
        signal=signal,
        account_equity=100_000,
        available_funds=100,
        atr=None,
    )
    assert not blocked.accepted
    assert "margin_budget_below_one_lot" in blocked.reason_codes


def test_adverse_slippage_rounds_against_trader_and_gap_stop_uses_open() -> None:
    assert adverse_fill_price(
        100.1, quantity_delta=1, slippage_points=0.2, price_tick=0.5
    ) == 100.5
    assert adverse_fill_price(
        100.1, quantity_delta=-1, slippage_points=0.2, price_tick=0.5
    ) == 99.5

    manager = ExitManager([StructureStopRule(grade_tighten=False)])
    manager.on_entry(
        direction=SignalDirection.LONG,
        entry_price=100,
        initial_stop_price=95,
    )
    exit_signal = manager.check(
        bar_end_time=NOW,
        open=90,
        high=92,
        low=89,
        close=91,
    )
    assert exit_signal is not None
    engine = SimulatedExecutionEngine(slippage_points=1, price_tick=1)
    engine.execute(_signal(price=100, target=1))
    fill = engine.execute(_signal(price=exit_signal.exit_price, target=0))
    assert exit_signal.exit_price == 90
    assert fill is not None and fill.fill_price == 89


def test_exit_priority_is_protection_then_structure_then_signal_then_momentum() -> None:
    manager = ExitManager(
        [
            ChanDivergenceExitRule(require_second_level_confirm=False),
            OppositeSignalRule(),
            StructureInvalidationExitRule(),
            StructureStopRule(grade_tighten=False),
        ],
        strict=True,
    )
    snapshot = ChanExitSnapshot(
        macd_areas=[100, 70],
        is_new_high_low=True,
        momentum_status="confirmed",
        momentum_confirmed=True,
    )
    manager.on_entry(
        direction=SignalDirection.LONG,
        entry_price=100,
        initial_stop_price=90,
        invalidation_price=95,
    )
    result = manager.check(
        bar_end_time=NOW,
        open=94,
        high=96,
        low=93,
        close=94,
        opposite_signal_triggered=True,
        chan_snapshot=snapshot,
    )
    assert result is not None
    assert result.reason_code == "structure_invalidated"

    manager.on_entry(
        direction=SignalDirection.LONG,
        entry_price=100,
        initial_stop_price=90,
        invalidation_price=80,
    )
    result = manager.check(
        bar_end_time=NOW,
        open=100,
        high=102,
        low=99,
        close=101,
        opposite_signal_triggered=True,
        chan_snapshot=snapshot,
    )
    assert result is not None
    assert result.reason_code == "opposite_signal_flat"


def test_aggregation_keeps_adjusted_and_actual_contract_prices() -> None:
    rows = []
    for minute in range(1, 6):
        raw = 100 + minute
        rows.append(
            {
                "datetime": pd.Timestamp(f"2025-01-02 09:0{minute}"),
                "open": raw + 10,
                "high": raw + 11,
                "low": raw + 9,
                "close": raw + 10,
                "raw_open": raw,
                "raw_high": raw + 1,
                "raw_low": raw - 1,
                "raw_close": raw,
                "adjustment_points": 10,
                "volume": 1,
                "open_interest": 1,
                "active_symbol": "RB2501",
                "flags": 0,
            }
        )
    result = aggregate_continuous_1m_to_5m(pd.DataFrame(rows))
    assert result.iloc[0]["open"] == 111
    assert result.iloc[0]["raw_open"] == 101
    assert result.iloc[0]["raw_close"] == 105
    assert result.iloc[0]["adjustment_points"] == 10


def test_rollover_closes_old_contract_and_resets_kernel(monkeypatch) -> None:
    kernel = _RolloverKernel()
    monkeypatch.setattr(
        "chan_futures.backtest.make_runtime_decision_kernel",
        lambda *args, **kwargs: kernel,
    )
    frame = pd.DataFrame(
        [
            _production_bar("2025-01-02 09:15", "RB2501", raw=100, adjustment=10),
            _production_bar("2025-01-02 09:30", "RB2505", raw=120, adjustment=0),
        ]
    )
    config = StrategyConfig(
        exits=[],
        risk=RiskParams(max_abs_position=1),
        sizing=SizingParams(method="fixed", lots=1),
        execution=ExecutionParams(
            fee_points=0,
            slippage_points=1,
            price_tick=1,
        ),
        production=ProductionParams(
            enabled=True,
            use_raw_execution_prices=True,
            close_on_rollover=True,
            reset_structure_on_rollover=True,
            warmup_bars=0,
        ),
    )
    result = run_backtest(config, frame=frame)
    assert len(result.trades) == 1
    assert result.trades[0]["exit_reason"] == "contract_rollover"
    assert result.trades[0]["exit_price"] == 99
    assert kernel.reset_times == [pd.Timestamp("2025-01-02 09:30")]


def test_production_warmup_includes_active_contract_start() -> None:
    frame = pd.DataFrame(
        {
            "active_symbol": ["A"] * 4 + ["B"] * 8,
        }
    )
    assert _replay_start_index(frame, decision_idx=10, minimum_warmup_bars=3) == 4


def _decision(entry: float, stop: float) -> SignalDecision:
    return SignalDecision(
        decision_id="d1",
        event_id="e1",
        policy_id="p6",
        accepted=True,
        reason_codes=(),
        entry_price_hint=entry,
        invalidation_price=stop,
        initial_stop_price=stop,
        position_size_hint=1,
        decided_at=NOW,
        setup_invalidation_price=stop,
        execution_stop_price=stop,
    )


def _signal(*, price: float, target: int) -> StrategySignal:
    return StrategySignal(
        timestamp=NOW,
        action="open_long" if target else "close_long",
        target_position=target,
        price=price,
        reason="test",
        bsp_type="1",
        bsp_bi_idx=0,
        bsp_klu_idx=0,
    )


def _production_bar(timestamp: str, symbol: str, *, raw: float, adjustment: float):
    return {
        "datetime": pd.Timestamp(timestamp),
        "open": raw + adjustment,
        "high": raw + adjustment + 2,
        "low": raw + adjustment - 2,
        "close": raw + adjustment,
        "raw_open": raw,
        "raw_high": raw + 2,
        "raw_low": raw - 2,
        "raw_close": raw,
        "adjustment_points": adjustment,
        "active_symbol": symbol,
    }


class _Extractor:
    contract = "RB_MAIN"

    def extract(self, *args, **kwargs):
        return None


class _RolloverKernel:
    def __init__(self) -> None:
        self.extractor = _Extractor()
        self.calls = 0
        self.reset_times: list[object] = []

    def observe_structure(self, **kwargs):
        return None

    def evaluate_bar(self, **kwargs):
        self.calls += 1
        if self.calls != 1:
            return None
        signal = _signal(price=kwargs["price"], target=1)
        return TradeIntent(signal=signal, decision=_decision(signal.price, 95))

    def build_exit_snapshot(self, *args, **kwargs):
        return ChanExitSnapshot()

    def reset_contract_state(self, *, start_time=None, active_symbol=None):
        self.reset_times.append(start_time)

    @property
    def decision_trace(self):
        return ()

    @property
    def decomposition_transitions(self):
        return ()

    @property
    def multi_level_audit(self):
        return ()
