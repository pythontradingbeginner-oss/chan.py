from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from chan_futures.decision_pipeline import (
    DecisionMode,
    DecisionPipeline,
    DecisionPipelineConfig,
)
from chan_futures.graded_strategy import GradeFilterConfig, GradedChanStrategy
from chan_futures.sizing import FixedFractionalSizer
from chan_futures.strategy import StrategySignal
from signal_core.models import (
    ScoreGrade,
    SignalAssessment,
    SignalDirection,
    SignalEvent,
    SignalState,
)
from strategy_policy.exit_rules import ExitContext, StructureStopRule


NOW = datetime(2024, 9, 26, 13, 45)


@pytest.mark.parametrize(
    (
        "bsp_type",
        "direction",
        "reference",
        "structural",
        "related_bsp1",
        "zs_high",
        "zs_low",
        "expected_setup",
        "expected_stop",
    ),
    [
        ("1", SignalDirection.LONG, 4231.0, 4225.0, None, None, None, 4225.0, 4225.0),
        ("1", SignalDirection.SHORT, 3509.0, 3517.0, None, None, None, 3517.0, 3517.0),
        ("2", SignalDirection.SHORT, 3529.0, 3540.0, 3572.0, None, None, 3572.0, 3540.0),
        ("3a", SignalDirection.LONG, 3527.0, 3524.0, None, 3484.0, 3381.0, 3484.0, 3524.0),
        ("3a", SignalDirection.SHORT, 4233.0, 4239.0, None, 4369.0, 4279.0, 4279.0, 4239.0),
    ],
)
def test_p0_double_anchor_contract(
    bsp_type,
    direction,
    reference,
    structural,
    related_bsp1,
    zs_high,
    zs_low,
    expected_setup,
    expected_stop,
) -> None:
    event = _event(
        bsp_type=bsp_type,
        direction=direction,
        reference=reference,
        structural=structural,
        related_bsp1=related_bsp1,
        zs_high=zs_high,
        zs_low=zs_low,
    )
    decision = _pipeline().evaluate(_signal(event), event, _assessment(event))

    assert decision.accepted
    assert decision.setup_invalidation_price == expected_setup
    assert decision.execution_stop_price == expected_stop
    assert decision.invalidation_price == expected_setup
    assert decision.initial_stop_price == expected_stop


def test_strict_rejects_missing_setup_anchor_and_wrong_side_stop() -> None:
    missing = _event(
        bsp_type="2",
        direction=SignalDirection.LONG,
        reference=100.0,
        structural=95.0,
    )
    wrong_side = _event(
        bsp_type="1",
        direction=SignalDirection.LONG,
        reference=100.0,
        structural=101.0,
    )

    missing_decision = _pipeline().evaluate(
        _signal(missing), missing, _assessment(missing)
    )
    wrong_decision = _pipeline().evaluate(
        _signal(wrong_side), wrong_side, _assessment(wrong_side)
    )

    assert not missing_decision.accepted
    assert "setup_invalidation_unavailable" in missing_decision.reason_codes
    assert not wrong_decision.accepted
    assert "execution_stop_wrong_side" in wrong_decision.reason_codes
    assert "setup_invalidation_wrong_side" in wrong_decision.reason_codes


def test_fixed_fractional_sizer_uses_equity_stop_distance_and_zero_floor() -> None:
    sizer = FixedFractionalSizer(
        capital=100_000.0,
        risk_pct=0.01,
        contract_multiplier=10.0,
    )

    assert sizer.calculate(initial_stop_price=90.0, entry_price=100.0) == 10
    assert sizer.calculate(
        initial_stop_price=90.0,
        entry_price=100.0,
        account_equity=50_000.0,
    ) == 5
    assert sizer.calculate(initial_stop_price=800.0, entry_price=1000.0) == 0
    assert sizer.calculate(initial_stop_price=100.0, entry_price=100.0) == 0


@pytest.mark.parametrize(
    ("direction", "structural", "expected_target"),
    [
        (SignalDirection.LONG, 90.0, 3),
        (SignalDirection.SHORT, 110.0, -3),
    ],
)
def test_shared_decision_size_becomes_signed_executable_target(
    direction: SignalDirection,
    structural: float,
    expected_target: int,
) -> None:
    event = _event(
        bsp_type="1",
        direction=direction,
        reference=100.0,
        structural=structural,
    )
    pipeline = _pipeline(
        sizer=FixedFractionalSizer(
            capital=100_000.0,
            risk_pct=0.01,
            contract_multiplier=10.0,
        ),
        max_abs_position=3,
    )
    wrapper = GradedChanStrategy(
        GradeFilterConfig(policy_mode=DecisionMode.QINGPAI_STRICT.value),
        decision_pipeline=pipeline,
    )

    result = wrapper._build_result(_signal(event), event, _assessment(event))

    assert result.accepted
    assert result.decision.position_size_hint == 3.0
    assert result.signal.target_position == expected_target


def test_sizing_rejects_when_risk_budget_cannot_fund_one_lot() -> None:
    event = _event(
        bsp_type="1",
        direction=SignalDirection.LONG,
        reference=1000.0,
        structural=800.0,
    )
    decision = _pipeline(
        sizer=FixedFractionalSizer(
            capital=100_000.0,
            risk_pct=0.01,
            contract_multiplier=10.0,
        ),
        max_abs_position=100,
    ).evaluate(_signal(event), event, _assessment(event))

    assert not decision.accepted
    assert decision.position_size_hint == 0.0
    assert "risk_budget_below_one_lot" in decision.reason_codes


def test_strict_rechecks_stop_side_against_actual_order_price() -> None:
    event = _event(
        bsp_type="1",
        direction=SignalDirection.LONG,
        reference=100.0,
        structural=95.0,
    )
    signal = replace(_signal(event), price=94.0)
    decision = _pipeline(
        sizer=FixedFractionalSizer(
            capital=100_000.0,
            risk_pct=0.01,
            contract_multiplier=10.0,
        )
    ).evaluate(signal, event, _assessment(event))

    assert not decision.accepted
    assert "execution_stop_wrong_side_at_entry" in decision.reason_codes
    assert "setup_invalidation_wrong_side_at_entry" in decision.reason_codes


def test_structure_stop_uses_execution_stop_and_gap_open_price() -> None:
    rule = StructureStopRule(grade_tighten=True)
    not_hit = ExitContext(
        bar_end_time=NOW,
        open=100.0,
        high=101.0,
        low=96.0,
        close=97.0,
        direction=SignalDirection.LONG,
        entry_price=100.0,
        initial_stop_price=95.0,
        invalidation_price=98.0,
        bi_begin_price=97.0,
        entry_grade="weak",
    )
    gap_hit = ExitContext(
        bar_end_time=NOW,
        open=92.0,
        high=94.0,
        low=90.0,
        close=91.0,
        direction=SignalDirection.LONG,
        entry_price=100.0,
        initial_stop_price=95.0,
        invalidation_price=90.0,
        entry_grade="weak",
    )

    assert rule.check(not_hit) is None
    signal = rule.check(gap_hit)
    assert signal is not None
    assert signal.exit_price == 92.0
    assert "跳空" in signal.description


def test_cta_open_fill_installs_the_same_decision_anchors() -> None:
    pytest.importorskip("vnpy_ctastrategy")
    from types import SimpleNamespace

    from vnpy.trader.constant import Offset
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy

    event, decision = _accepted_sized_plan()
    capture = _ExitManagerCapture()
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._entry_volume = 0
    strategy._entry_price = 0.0
    strategy._entry_direction_str = "long"
    strategy._entry_grade = "standard"
    strategy._entry_bar = 10
    strategy._pending_entry_context = SimpleNamespace(event=event, decision=decision)
    strategy._exit_manager = capture
    strategy._config = None
    strategy.write_log = lambda message: None
    strategy.put_event = lambda: None

    strategy.on_trade(SimpleNamespace(offset=Offset.OPEN, volume=3, price=100.0))

    assert strategy._entry_volume == 3
    assert capture.entry_kwargs["initial_stop_price"] == 90.0
    assert capture.entry_kwargs["invalidation_price"] == 90.0


def test_live_open_fill_installs_the_same_decision_anchors_and_volume() -> None:
    pytest.importorskip("vnpy")
    from types import SimpleNamespace

    from vnpy.trader.constant import Direction, Offset
    from vnpy_chan.live_engine import LiveTradingEngine

    event, decision = _accepted_sized_plan()
    capture = _ExitManagerCapture()
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._pending_order_ids = {"SIM.1"}
    engine._pending_entry_contexts = {
        "SIM.1": {
            "event": event,
            "decision": decision,
            "grade": "standard",
            "signal_key": event.signal_key,
            "planned_volume": 3,
            "filled_volume": 0,
        }
    }
    engine._position = None
    engine._bar_count = 10
    engine._exit_manager = capture
    engine._log = lambda message: None

    trade = SimpleNamespace(
        vt_orderid="SIM.1",
        offset=Offset.OPEN,
        direction=Direction.LONG,
        volume=3,
        price=100.0,
    )
    engine._on_trade(SimpleNamespace(data=trade))

    assert engine._position.volume == 3
    assert engine._position.initial_stop_price == 90.0
    assert capture.entry_kwargs["initial_stop_price"] == 90.0
    assert capture.entry_kwargs["invalidation_price"] == 90.0


def _pipeline(
    *,
    sizer: FixedFractionalSizer | None = None,
    max_abs_position: int | None = None,
) -> DecisionPipeline:
    return DecisionPipeline(
        DecisionPipelineConfig(
            mode=DecisionMode.QINGPAI_STRICT,
            min_grade=ScoreGrade.STANDARD,
            accepted_bsp_types=frozenset({"1", "1p", "2", "2s", "3a", "3b"}),
            max_abs_position=max_abs_position,
        ),
        sizer=sizer,
    )


def _accepted_sized_plan():
    event = _event(
        bsp_type="1",
        direction=SignalDirection.LONG,
        reference=100.0,
        structural=90.0,
    )
    decision = _pipeline(
        sizer=FixedFractionalSizer(
            capital=100_000.0,
            risk_pct=0.01,
            contract_multiplier=10.0,
        ),
        max_abs_position=3,
    ).evaluate(_signal(event), event, _assessment(event))
    assert decision.accepted
    return event, decision


class _ExitManagerCapture:
    def __init__(self) -> None:
        self.entry_kwargs = None

    def on_entry(self, **kwargs) -> None:
        self.entry_kwargs = kwargs

    def on_close(self) -> None:
        pass


def _signal(event: SignalEvent) -> StrategySignal:
    target = 1 if event.direction == SignalDirection.LONG else -1
    return StrategySignal(
        timestamp=NOW,
        action="open_long" if target > 0 else "open_short",
        target_position=target,
        price=event.reference_price,
        reason="p2-test",
        bsp_type=event.primary_bsp,
        bsp_bi_idx=event.bi_idx,
        bsp_klu_idx=100,
    )


def _event(
    *,
    bsp_type: str,
    direction: SignalDirection,
    reference: float,
    structural: float | None,
    related_bsp1: float | None = None,
    zs_high: float | None = None,
    zs_low: float | None = None,
) -> SignalEvent:
    return SignalEvent(
        event_id=f"event-{bsp_type}-{direction.value}",
        signal_key=f"key-{bsp_type}-{direction.value}",
        revision=0,
        symbol="RB",
        contract="RB_MAIN",
        timeframe="15m",
        bar_end_time=NOW,
        available_at=NOW,
        state=SignalState.CONFIRMED,
        direction=direction,
        primary_bsp=bsp_type,
        bsp_types=(bsp_type,),
        reference_price=reference,
        bi_idx=20,
        seg_idx=3,
        bi_begin_price=None,
        zs_high=zs_high,
        zs_low=zs_low,
        parent_event_id=None,
        feature_schema_version="v0",
        features={"divergence_rate": 0.8, "zs_cnt": 1, "bi_amp": 20.0},
        chan_version="v3_public",
        data_run_id="test",
        structural_price=structural,
        related_bsp1_bi_idx=5 if related_bsp1 is not None else None,
        related_bsp1_price=related_bsp1,
    )


def _assessment(event: SignalEvent) -> SignalAssessment:
    return SignalAssessment(
        assessment_id=f"assessment-{event.event_id}",
        event_id=event.event_id,
        scorer_id="test",
        scorer_version="1",
        structural_score=0.7,
        predictive_score=None,
        grade=ScoreGrade.STANDARD,
        hard_blockers=(),
        component_scores={},
        computed_at=NOW,
    )
