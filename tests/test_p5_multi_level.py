from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from Common.CEnum import KL_TYPE
from chan_futures.backtest import run_backtest
from chan_futures.config import ChanParams, MultiLevelParams
from chan_futures.config_loader import load_config
from chan_futures.interval_filter import IntervalConfirmer
from chan_futures.multi_level import (
    MultiLevelDecisionEngine,
    ParentDirection,
    ParentStructureSnapshot,
    SubLevelSignalSnapshot,
    market_timestamp,
)
from chan_futures.strategy import StrategySignal
from chan_futures.trade_intent import TradeIntent
from signal_core.models import SignalDecision, SignalDirection


class _FakeBi:
    def __init__(self, begin: datetime, end: datetime) -> None:
        self.is_sure = True
        self._begin = SimpleNamespace(time=begin)
        self._end = SimpleNamespace(time=end)

    def get_begin_klu(self):
        return self._begin

    def get_end_klu(self):
        return self._end


class _FakeChan:
    def __init__(self, begin: datetime, end: datetime) -> None:
        self._level = SimpleNamespace(bi_list=[_FakeBi(begin, end)])

    def __getitem__(self, index: int):
        assert index == 0
        return self._level


def _engine() -> MultiLevelDecisionEngine:
    return MultiLevelDecisionEngine(
        enabled=True,
        code="RB_TEST",
        chan_config=ChanParams().to_dict(),
        parent_kl_type=KL_TYPE.K_60M,
        child_kl_type=KL_TYPE.K_5M,
        parent_level_name="K_60M",
        child_level_name="K_5M",
        require_parent_direction=True,
        require_confirmed_parent=True,
        require_child_confirmation=True,
    )


def _intent(*, target_position: int = 1) -> TradeIntent:
    decided_at = datetime(2024, 1, 2, 10, 15)
    direction = SignalDirection.LONG if target_position >= 0 else SignalDirection.SHORT
    event = SimpleNamespace(
        event_id="event-1",
        signal_key="signal-1",
        bi_idx=0,
        direction=direction,
    )
    return TradeIntent(
        signal=StrategySignal(
            timestamp=decided_at,
            action="open_long" if target_position > 0 else "close_long",
            target_position=target_position,
            price=3500.0,
            reason="chan_bsp",
            bsp_type="1",
            bsp_bi_idx=0,
            bsp_klu_idx=10,
        ),
        decision=SignalDecision(
            decision_id="decision-1",
            event_id="event-1",
            policy_id="test",
            accepted=True,
            reason_codes=(),
            entry_price_hint=3500.0,
            invalidation_price=3480.0,
            initial_stop_price=3480.0,
            position_size_hint=1.0,
            decided_at=decided_at,
            setup_invalidation_price=3480.0,
            execution_stop_price=3485.0,
        ),
        event=event,
    )


def _record_parent(
    engine: MultiLevelDecisionEngine,
    *,
    direction: ParentDirection = ParentDirection.BULLISH,
    available_at: datetime = datetime(2024, 1, 2, 10, 0),
) -> None:
    engine.parent_tracker.record(
        ParentStructureSnapshot(
            snapshot_id="parent-1",
            level="K_60M",
            direction=direction,
            structure_kind="segment",
            structure_idx=3,
            structure_begin_time=datetime(2024, 1, 2, 9, 0),
            structure_end_time=datetime(2024, 1, 2, 10, 0),
            available_at=available_at,
            is_confirmed=True,
        )
    )


def _record_child(
    engine: MultiLevelDecisionEngine,
    *,
    available_at: datetime,
) -> None:
    engine.child_tracker.record(
        SubLevelSignalSnapshot(
            signal_id="child-1",
            level="K_5M",
            bi_idx=99,
            direction="long",
            bsp_types=("1",),
            signal_time=datetime(2024, 1, 2, 9, 55),
            bi_begin_time=datetime(2024, 1, 2, 9, 30),
            bi_end_time=datetime(2024, 1, 2, 9, 55),
            available_at=available_at,
            is_confirmed=True,
        )
    )


def test_visible_parent_and_child_confirm_entry_by_timestamps() -> None:
    engine = _engine()
    _record_parent(engine)
    _record_child(engine, available_at=datetime(2024, 1, 2, 10, 5))
    context = engine.assess(
        _intent(),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )

    assert context.accepted
    assert context.time_honest
    assert context.parent_action == "pass"
    assert context.child_confirmed
    assert context.child_matches[0].bi_idx == 99


def test_child_confirmed_after_decision_is_not_visible() -> None:
    engine = _engine()
    _record_parent(engine)
    _record_child(engine, available_at=datetime(2024, 1, 2, 10, 30))
    context = engine.assess(
        _intent(),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )

    assert not context.accepted
    assert not context.child_confirmed
    assert context.child_matches == ()
    assert context.child_last_observed_at is None
    assert "sublevel_confirmation_not_available_at_decision" in context.reason_codes


def test_parent_confirmed_after_decision_is_not_visible() -> None:
    engine = _engine()
    _record_parent(engine, available_at=datetime(2024, 1, 2, 10, 30))
    _record_child(engine, available_at=datetime(2024, 1, 2, 10, 5))
    context = engine.assess(
        _intent(),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )

    assert context.parent_snapshot is None
    assert context.parent_last_observed_at is None
    assert "parent_not_available_at_decision" in context.reason_codes


def test_interval_confirmer_uses_time_not_cross_level_idx() -> None:
    child = SubLevelSignalSnapshot(
        signal_id="child-far-idx",
        level="K_5M",
        bi_idx=999_999,
        direction="long",
        bsp_types=("1",),
        signal_time=datetime(2024, 1, 2, 9, 55),
        bi_begin_time=datetime(2024, 1, 2, 9, 30),
        bi_end_time=datetime(2024, 1, 2, 9, 55),
        available_at=datetime(2024, 1, 2, 10, 5),
        is_confirmed=True,
    )
    result = IntervalConfirmer().confirm(
        _FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        0,
        "long",
        decision_time=datetime(2024, 1, 2, 10, 15),
        child_signals=(child,),
    )

    assert result.confirmed
    assert result.time_honest
    assert result.matched_signal_times == (child.signal_time,)


def test_parent_direction_conflict_rejects_and_preserves_stop_anchors() -> None:
    engine = _engine()
    _record_parent(engine, direction=ParentDirection.BEARISH)
    _record_child(engine, available_at=datetime(2024, 1, 2, 10, 5))
    result = engine.apply(
        _intent(),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )

    assert not result.accepted
    assert result.decision.entry_price_hint is None
    assert result.decision.position_size_hint is None
    assert result.decision.setup_invalidation_price == 3480.0
    assert result.decision.execution_stop_price == 3485.0
    assert "parent_direction_conflict" in result.decision.reason_codes


def test_flattening_intent_bypasses_entry_confirmation() -> None:
    context = _engine().assess(
        _intent(target_position=0),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )

    assert context.accepted
    assert not context.parent_required
    assert not context.child_required


def test_multi_level_trace_never_contains_future_evidence() -> None:
    engine = _engine()
    _record_parent(engine)
    _record_child(engine, available_at=datetime(2024, 1, 2, 10, 5))
    result = engine.apply(
        _intent(),
        current_chan=_FakeChan(
            datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
        ),
        decision_time=datetime(2024, 1, 2, 10, 15),
    )
    trace = result.to_trace_record()

    assert trace.multi_level_time_honest
    assert market_timestamp(trace.parent_available_at) <= market_timestamp(trace.timestamp)
    assert trace.child_match_ids == ("child-1",)
    assert trace.child_confirmed


def test_three_runtime_lanes_produce_identical_multi_level_trace() -> None:
    traces = []
    for _runtime_name in ("backtest", "vnpy_cta", "live"):
        engine = _engine()
        _record_parent(engine)
        _record_child(engine, available_at=datetime(2024, 1, 2, 10, 5))
        result = engine.apply(
            _intent(),
            current_chan=_FakeChan(
                datetime(2024, 1, 2, 9, 0), datetime(2024, 1, 2, 10, 0)
            ),
            decision_time=datetime(2024, 1, 2, 10, 15),
        )
        traces.append(result.to_trace_record())

    assert traces[0] == traces[1] == traces[2]
    assert traces[0].parent_direction == "bullish"
    assert traces[0].child_match_ids == ("child-1",)


def test_config_tuple_survives_dataclass_replace() -> None:
    params = replace(
        MultiLevelParams(),
        enabled=True,
        accepted_child_bsp_types=("1", "2"),
    )
    assert params.accepted_child_bsp_types == ("1", "2")


def test_real_rb_replay_only_uses_visible_cross_level_evidence(tmp_path) -> None:
    result = run_backtest(
        load_config("configs/rb_15m_qingpai_strict.yaml"),
        limit=400,
    )

    assert result.multi_level_audit
    assert all(context.time_honest for context in result.multi_level_audit)
    for context in result.multi_level_audit:
        decision_time = market_timestamp(context.decision_time)
        if context.parent_snapshot is not None:
            assert market_timestamp(context.parent_snapshot.available_at) <= decision_time
        assert all(
            market_timestamp(match.available_at) <= decision_time
            for match in context.child_matches
        )
    result.save(tmp_path)
    assert (tmp_path / "multi_level_decisions.csv").exists()
