from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chan_futures.config_loader import load_config, make_decision_pipeline
from chan_futures.decision_pipeline import (
    DecisionMode,
    DecisionPipeline,
    DecisionPipelineConfig,
)
from chan_futures.strategy import StrategySignal
from chan_futures.graded_strategy import GradeFilterConfig, GradedChanStrategy
from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDirection,
    SignalEvent,
    SignalState,
)
from signal_scoring import assess_event


NOW = datetime(2024, 9, 26, 13, 45)


def test_qingpai_strict_rejects_hard_blocker_but_legacy_preserves_history() -> None:
    event = _event(primary_bsp="1", blockers_feature=True)
    assessment = _assessment(event, hard_blockers=("no_divergence",))
    signal = _signal(target_position=1, bsp_type="1")

    strict = _pipeline(DecisionMode.QINGPAI_STRICT).evaluate(signal, event, assessment)
    legacy = _pipeline(DecisionMode.LEGACY).evaluate(signal, event, assessment)

    assert not strict.accepted
    assert strict.reason_codes == ("hard_blocker:no_divergence",)
    assert legacy.accepted


@pytest.mark.parametrize("mode", list(DecisionMode))
def test_direction_mismatch_is_rejected_in_every_mode(mode: DecisionMode) -> None:
    event = _event(direction=SignalDirection.SHORT)
    decision = _pipeline(mode).evaluate(
        _signal(target_position=1), event, _assessment(event)
    )

    assert not decision.accepted
    assert "signal_direction_mismatch" in decision.reason_codes


def test_event_must_be_available_when_the_decision_is_made() -> None:
    event = _event(available_at=NOW + timedelta(minutes=15))
    decision = _pipeline(DecisionMode.QINGPAI_STRICT).evaluate(
        _signal(target_position=1), event, _assessment(event)
    )

    assert not decision.accepted
    assert "event_not_available" in decision.reason_codes


def test_strict_fails_closed_without_event_while_legacy_passes_through() -> None:
    signal = _signal(target_position=1)

    strict = _pipeline(DecisionMode.QINGPAI_STRICT).evaluate(signal, None, None)
    legacy = _pipeline(DecisionMode.LEGACY).evaluate(signal, None, None)

    assert not strict.accepted
    assert strict.reason_codes == ("signal_event_unavailable",)
    assert legacy.accepted
    assert legacy.reason_codes == ("legacy_ungraded_passthrough",)


def test_p0_rb15_007_t3_uses_return_extreme_instead_of_confirmation_close() -> None:
    event = _event(
        primary_bsp="3b",
        reference_price=3241.0,
        structural_price=3211.0,
        zs_high=3219.0,
        zs_low=3123.0,
        features={"bi_amp": 77.0, "bsp3_bi_amp": 77.0},
    )

    assessment = assess_event(event)
    decision = _pipeline(DecisionMode.QINGPAI_STRICT).evaluate(
        _signal(target_position=1, bsp_type="3b"), event, assessment
    )

    assert "not_above_zs" in assessment.hard_blockers
    assert assessment.component_scores["structural_price_available"] == 1.0
    assert not decision.accepted


def test_strict_profile_config_builds_the_shared_kernel() -> None:
    config = load_config("configs/rb_15m_qingpai_strict.yaml")
    pipeline = make_decision_pipeline(config)

    assert pipeline.config.mode == DecisionMode.QINGPAI_STRICT
    assert pipeline.config.policy_id == "rb_15m_qingpai_strict_v1"
    assert pipeline.config.allow_short


def test_candidate_rejection_is_released_for_confirmation_retry() -> None:
    event = _event(state=SignalState.CANDIDATE)
    assessment = _assessment(event)
    signal = _signal(target_position=1)
    wrapper = GradedChanStrategy(
        GradeFilterConfig(policy_mode=DecisionMode.QINGPAI_STRICT.value)
    )
    consumed_key = (signal.bsp_bi_idx, signal.bsp_klu_idx, signal.bsp_type, True)
    wrapper._inner._consumed_keys.add(consumed_key)

    result = wrapper._build_result(signal, event, assessment)

    assert not result.accepted
    assert "signal_not_confirmed" in result.decision.reason_codes
    assert consumed_key not in wrapper._inner._consumed_keys


def test_live_open_order_uses_signal_direction_open_offset_and_gateway() -> None:
    pytest.importorskip("vnpy")
    from vnpy.trader.constant import Direction, Exchange, Offset
    from vnpy_chan.live_engine import LiveTradingEngine

    class _Contract:
        gateway_name = "SIM"

    class _MainEngine:
        request = None
        gateway_name = None

        def get_contract(self, vt_symbol):
            assert vt_symbol == "RB2505.SHFE"
            return _Contract()

        def send_order(self, request, gateway_name):
            self.request = request
            self.gateway_name = gateway_name
            return "SIM.1"

        def write_log(self, message, source):
            pass

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.main_engine = _MainEngine()
    engine.engine_name = "ChanLive"
    engine._vt_symbol = "RB2505.SHFE"
    engine._exchange = Exchange.SHFE
    engine._pending_order_ids = set()

    engine._open_position(_signal(target_position=-1), "1", "standard", "event-1")

    request = engine.main_engine.request
    assert request.direction == Direction.SHORT
    assert request.offset == Offset.OPEN
    assert request.volume == 1
    assert engine.main_engine.gateway_name == "SIM"
    assert engine._pending_order_ids == {"SIM.1"}


def test_vnpy_execution_engine_opens_short_and_rejects_direct_reversal() -> None:
    pytest.importorskip("vnpy")
    from vnpy.trader.constant import Direction, Exchange, Offset
    from vnpy_chan.live_execution import VnpyExecutionEngine

    class _Contract:
        exchange = Exchange.SHFE
        gateway_name = "SIM"

    class _MainEngine:
        request = None

        def get_contract(self, vt_symbol):
            return _Contract()

        def send_order(self, request, gateway_name):
            self.request = request
            assert gateway_name == "SIM"
            return "SIM.2"

    main_engine = _MainEngine()
    engine = VnpyExecutionEngine()
    engine.configure(main_engine, "RB2505.SHFE")
    engine.execute(_signal(target_position=-1))

    assert main_engine.request.direction == Direction.SHORT
    assert main_engine.request.offset == Offset.OPEN
    assert set(engine.pending_orders) == {"SIM.2"}

    engine.state.position = 1
    with pytest.raises(ValueError, match="先平仓再开仓"):
        engine.execute(_signal(target_position=-1))


def test_vnpy_execution_engine_applies_partial_close_fills_incrementally() -> None:
    pytest.importorskip("vnpy")
    from types import SimpleNamespace
    from vnpy.trader.constant import Direction, Exchange
    from vnpy_chan.live_execution import VnpyExecutionEngine

    class _Contract:
        exchange = Exchange.SHFE
        gateway_name = "SIM"

    class _MainEngine:
        def get_contract(self, vt_symbol):
            return _Contract()

        def send_order(self, request, gateway_name):
            return "SIM.3"

    engine = VnpyExecutionEngine(fee_points=1.0, slippage_points=0.0)
    engine.configure(_MainEngine(), "RB2505.SHFE")
    engine.state.position = 2
    engine.state.avg_price = 100.0
    close_signal = StrategySignal(
        timestamp=NOW,
        action="close_long",
        target_position=0,
        price=110.0,
        reason="test",
        bsp_type="1",
        bsp_bi_idx=1,
        bsp_klu_idx=1,
    )
    engine.execute(close_signal)
    trade = SimpleNamespace(
        vt_orderid="SIM.3",
        price=110.0,
        volume=1,
        direction=Direction.SHORT,
        symbol="RB2505",
    )

    engine.on_trade_filled(trade)
    assert engine.state.position == 1
    assert engine.state.realized_points == 9.0
    assert engine.has_pending_orders

    engine.on_trade_filled(trade)
    assert engine.state.position == 0
    assert engine.state.realized_points == 18.0
    assert not engine.has_pending_orders


def _pipeline(mode: DecisionMode) -> DecisionPipeline:
    return DecisionPipeline(
        DecisionPipelineConfig(
            mode=mode,
            min_grade=ScoreGrade.STANDARD,
            accepted_bsp_types=frozenset({"1", "3b"}),
        )
    )


def _signal(target_position: int, bsp_type: str = "1") -> StrategySignal:
    return StrategySignal(
        timestamp=NOW,
        action="open_long" if target_position > 0 else "open_short",
        target_position=target_position,
        price=3241.0,
        reason="test",
        bsp_type=bsp_type,
        bsp_bi_idx=1560,
        bsp_klu_idx=100,
        active_symbol="RB2505",
    )


def _event(
    *,
    direction: SignalDirection = SignalDirection.LONG,
    primary_bsp: str = "1",
    available_at: datetime = NOW,
    reference_price: float = 3241.0,
    structural_price: float | None = 3230.0,
    zs_high: float | None = 3219.0,
    zs_low: float | None = 3123.0,
    features: dict | None = None,
    blockers_feature: bool = False,
    state: SignalState = SignalState.CONFIRMED,
) -> SignalEvent:
    event_features = features or {"divergence_rate": 0.8, "zs_cnt": 1, "bi_amp": 0.01}
    if blockers_feature:
        event_features = {**event_features, "divergence_rate": 1.3}
    return SignalEvent(
        event_id="RB_MAIN_15m_bi1560_long_1_r0",
        signal_key="RB_MAIN_15m_bi1560_long_1",
        revision=0,
        symbol="RB",
        contract="RB_MAIN",
        timeframe="15m",
        bar_end_time=NOW,
        available_at=available_at,
        state=state,
        direction=direction,
        primary_bsp=primary_bsp,
        bsp_types=(primary_bsp,),
        reference_price=reference_price,
        bi_idx=1560,
        seg_idx=245,
        bi_begin_price=3288.0,
        zs_high=zs_high,
        zs_low=zs_low,
        parent_event_id=None,
        feature_schema_version="v0",
        features=event_features,
        chan_version="v3_public",
        data_run_id="test",
        structural_price=structural_price,
    )


def _assessment(
    event: SignalEvent,
    *,
    hard_blockers: tuple[str, ...] = (),
) -> SignalAssessment:
    return SignalAssessment(
        assessment_id="assessment-1",
        event_id=event.event_id,
        scorer_id="test",
        scorer_version="1",
        structural_score=0.7,
        predictive_score=None,
        grade=ScoreGrade.STANDARD,
        hard_blockers=hard_blockers,
        component_scores={},
        computed_at=NOW,
    )
