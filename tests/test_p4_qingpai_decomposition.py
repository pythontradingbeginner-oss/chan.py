from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from strategy_policy.qingpai_decomposition import (
    CenterSnapshot,
    DecompositionLifecycle,
    DecompositionObservation,
    QingpaiDecomposer,
    QingpaiRegime,
    QingpaiStateMachine,
    StrokeSnapshot,
    StructureDirection,
    TransitionKind,
    classify_observation,
)


NOW = datetime(2024, 1, 1, 9, 0)
ROOT = Path(__file__).resolve().parents[1]


def _center(
    center_id: str,
    *,
    entry: int,
    begin: int,
    end: int,
    low: float,
    high: float,
    peak_low: float,
    peak_high: float,
    direction: StructureDirection = StructureDirection.BULLISH,
    start_price: float = 80.0,
    is_sure: bool = True,
) -> CenterSnapshot:
    return CenterSnapshot(
        center_id=center_id,
        entry_bi_idx=entry,
        begin_bi_idx=begin,
        end_bi_idx=end,
        direction=direction,
        start_price=start_price,
        low=low,
        high=high,
        peak_low=peak_low,
        peak_high=peak_high,
        is_sure=is_sure,
    )


def _observation(
    *centers: CenterSnapshot,
    strokes: tuple[StrokeSnapshot, ...] = (),
    at: datetime = NOW,
) -> DecompositionObservation:
    return DecompositionObservation(
        observed_at=at,
        available_at=at,
        centers=centers,
        strokes=strokes,
    )


def test_five_qingpai_regimes_have_distinct_geometry() -> None:
    base = _center(
        "c1", entry=0, begin=1, end=3,
        low=100.0, high=110.0, peak_low=90.0, peak_high=120.0,
    )
    extended = replace(base, end_bi_idx=7)
    trend_second = _center(
        "c2", entry=4, begin=5, end=7,
        low=130.0, high=140.0, peak_low=121.0, peak_high=150.0,
        start_price=95.0,
    )
    expansion_second = replace(trend_second, peak_low=105.0)
    overlapping_second = replace(
        expansion_second,
        low=108.0,
        high=118.0,
    )
    expanded_retrace = StrokeSnapshot(
        idx=8,
        direction=StructureDirection.BEARISH,
        begin_price=145.0,
        end_price=108.0,
        low=108.0,
        high=145.0,
    )
    direct_break = StrokeSnapshot(
        idx=8,
        direction=StructureDirection.BULLISH,
        begin_price=140.0,
        end_price=155.0,
        low=135.0,
        high=155.0,
    )
    late_retrace = replace(expanded_retrace, idx=9)

    assert classify_observation(_observation(base)).regime == QingpaiRegime.CONSOLIDATION
    assert classify_observation(_observation(extended)).regime == QingpaiRegime.EXTENSION
    assert (
        classify_observation(_observation(base, overlapping_second)).regime
        == QingpaiRegime.EXTENSION
    )
    assert (
        classify_observation(_observation(base, trend_second)).regime
        == QingpaiRegime.TREND
    )
    assert (
        classify_observation(_observation(base, expansion_second)).regime
        == QingpaiRegime.EXPANSION
    )
    assert (
        classify_observation(
            _observation(base, expansion_second, strokes=(expanded_retrace,))
        ).regime
        == QingpaiRegime.EXPANDED
    )
    assert (
        classify_observation(
            _observation(
                base,
                expansion_second,
                strokes=(direct_break, late_retrace),
            )
        ).regime
        == QingpaiRegime.EXPANSION
    )


def test_open_state_can_be_revised_but_closed_state_is_immutable() -> None:
    forming = _center(
        "c1", entry=0, begin=1, end=3,
        low=100.0, high=110.0, peak_low=90.0, peak_high=120.0,
        is_sure=False,
    )
    machine = QingpaiStateMachine(forming)

    opened = machine.update(_observation(forming))
    revised = machine.update(
        _observation(replace(forming, end_bi_idx=7, is_sure=True), at=NOW + timedelta(minutes=15))
    )
    closed = machine.close(
        observed_at=NOW + timedelta(minutes=30),
        available_at=NOW + timedelta(minutes=30),
        trigger="test_close",
    )
    assert closed is not None

    ignored = machine.update(
        _observation(
            replace(forming, end_bi_idx=11, is_sure=True),
            at=NOW + timedelta(minutes=45),
        )
    )

    assert opened.revision == 0
    assert revised.revision == 1
    assert revised.regime == QingpaiRegime.EXTENSION
    assert closed.revision == 2
    assert closed.lifecycle == DecompositionLifecycle.CLOSED
    assert ignored is closed
    assert [transition.kind for transition in machine.transitions] == [
        TransitionKind.OPEN,
        TransitionKind.REVISE,
        TransitionKind.CLOSE,
    ]
    assert [transition.revision for transition in machine.transitions] == [0, 1, 2]


def test_decomposer_archives_closed_state_and_uses_global_audit_sequence() -> None:
    bullish = _center(
        "bull", entry=0, begin=1, end=3,
        low=100.0, high=110.0, peak_low=90.0, peak_high=120.0,
    )
    bearish = _center(
        "bear", entry=4, begin=5, end=7,
        low=80.0, high=90.0, peak_low=70.0, peak_high=100.0,
        direction=StructureDirection.BEARISH,
        start_price=130.0,
    )
    decomposer = QingpaiDecomposer()
    decomposer.update(_observation(bullish))
    current = decomposer.update(
        _observation(bullish, bearish, at=NOW + timedelta(minutes=15))
    )

    assert current is not None
    assert current.direction == StructureDirection.BEARISH
    assert len(decomposer.closed_states) == 1
    frozen = decomposer.closed_states[0]
    assert frozen.lifecycle == DecompositionLifecycle.CLOSED
    assert frozen.direction == StructureDirection.BULLISH

    decomposer.update(
        _observation(
            bullish,
            replace(bearish, end_bi_idx=11),
            at=NOW + timedelta(minutes=30),
        )
    )
    assert decomposer.closed_states[0] is frozen
    assert [transition.sequence for transition in decomposer.transitions] == list(
        range(len(decomposer.transitions))
    )
    assert decomposer.transitions[-1].centers
    assert decomposer.transitions[-1].snapshot_fingerprint


def test_invalidated_only_center_closes_without_waiting_for_replacement() -> None:
    center = _center(
        "bull", entry=0, begin=1, end=3,
        low=100.0, high=110.0, peak_low=90.0, peak_high=120.0,
    )
    break_stroke = StrokeSnapshot(
        idx=4,
        direction=StructureDirection.BEARISH,
        begin_price=120.0,
        end_price=70.0,
        low=70.0,
        high=120.0,
    )
    decomposer = QingpaiDecomposer()
    decomposer.update(_observation(center))
    current = decomposer.update(
        _observation(
            replace(center, is_valid=False),
            strokes=(break_stroke,),
            at=NOW + timedelta(minutes=15),
        )
    )

    assert current is None
    assert len(decomposer.closed_states) == 1
    assert decomposer.closed_states[0].lifecycle == DecompositionLifecycle.CLOSED
    assert decomposer.transitions[-1].trigger == "entry_start_invalidated"


def test_nine_strokes_are_audit_upgrade_candidate_not_level_rewrite() -> None:
    center = _center(
        "c1", entry=0, begin=1, end=9,
        low=100.0, high=110.0, peak_low=90.0, peak_high=120.0,
    )
    classified = classify_observation(_observation(center))

    assert classified.regime == QingpaiRegime.EXTENSION
    assert classified.upgrade_candidate is True


def test_p4_contract_resolves_resegmentation_without_hiding_other_open_rules() -> None:
    contract = json.loads(
        (ROOT / "configs" / "qingpai_rule_contract_v1.json").read_text(
            encoding="utf-8"
        )
    )
    rules = {rule["id"] for rule in contract["rules"]}
    open_ids = {decision["id"] for decision in contract["open_decisions"]}
    resolved_ids = {decision["id"] for decision in contract["resolved_decisions"]}

    assert {"QP-DECOMP-003", "QP-DECOMP-004"} <= rules
    assert "OPEN-002" not in open_ids
    assert "OPEN-002" in resolved_ids
    assert "OPEN-001" in open_ids
