"""Tests for strategy_policy/exit_rules — all rules including V2 changes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from signal_core.models import SignalDirection
from strategy_policy.exit_rules import (
    ChanDivergenceExitRule,
    ChanExitSnapshot,
    ChanSegmentCompleteExitRule,
    ChanSmallTurnExitRule,
    ExitAction,
    ExitContext,
    ExitDirective,
    ExitManager,
    ExitRule,
    ExitSignal,
    FixedStopRule,
    MACDCrossRule,
    OppositeSignalRule,
    StructureStopRule,
    TimeStopRule,
    TrailingStopRule,
    TrailingState,
)

_NOW = datetime(2026, 7, 7, 14, 30)


# ── helpers ──────────────────────────────────────────────

def _ctx_long(**overrides) -> ExitContext:
    d = {
        "bar_end_time": _NOW,
        "open": 3805.0, "high": 3810.0, "low": 3795.0, "close": 3802.0,
        "direction": SignalDirection.LONG,
        "entry_price": 3800.0,
        "bars_since_entry": 10,
        "current_move": 2.0,
        "mfe": 20.0,
        "mae": -5.0,
    }
    d.update(overrides)
    return ExitContext(**d)


def _ctx_short(**overrides) -> ExitContext:
    d = {
        "bar_end_time": _NOW,
        "open": 3805.0, "high": 3810.0, "low": 3795.0, "close": 3802.0,
        "direction": SignalDirection.SHORT,
        "entry_price": 3800.0,
        "bars_since_entry": 10,
        "current_move": 0.0,
        "mfe": 15.0,
        "mae": -5.0,
    }
    d.update(overrides)
    return ExitContext(**d)


def _check(**overrides):
    d = {"bar_end_time": _NOW, "open": 3805.0, "high": 3810.0, "low": 3795.0, "close": 3802.0}
    d.update(overrides)
    return d


def _chan_snapshot(**overrides) -> ChanExitSnapshot:
    d = {}
    d.update(overrides)
    return ChanExitSnapshot(**d)


# ══════════════════════════════════════════════════════════
# TrailingState
# ══════════════════════════════════════════════════════════

class TestTrailingState:
    def test_initial_state(self):
        ts = TrailingState()
        assert ts.bars_since_entry == 0
        assert ts.best_favorable_move == 0.0

    def test_reset(self):
        ts = TrailingState()
        ts.bars_since_entry = 50
        ts.best_favorable_move = 100.0
        ts.reset()
        assert ts.bars_since_entry == 0
        assert ts.best_favorable_move == 0.0


# ══════════════════════════════════════════════════════════
# ExitContext
# ══════════════════════════════════════════════════════════

class TestExitContext:
    def test_is_long(self):
        assert _ctx_long().is_long is True
        assert _ctx_long(direction=SignalDirection.SHORT).is_long is False

    def test_sign(self):
        assert _ctx_long().sign == 1
        assert _ctx_long(direction=SignalDirection.SHORT).sign == -1

    def test_chan_none_by_default(self):
        assert _ctx_long().chan is None


# ══════════════════════════════════════════════════════════
# StructureStopRule
# ══════════════════════════════════════════════════════════

class TestStructureStopRule:
    def test_long_stop_triggered(self):
        rule = StructureStopRule()
        ctx = _ctx_long(low=3745.0, invalidation_price=3750.0)
        result = rule.check(ctx)
        assert result is not None
        assert result.reason_code == "structure_stop"
        assert result.exit_price == 3750.0

    def test_long_stop_no_anchors_returns_none(self):
        rule = StructureStopRule()
        ctx = _ctx_long(low=3745.0)
        assert rule.check(ctx) is None

    def test_long_stop_picks_tightest(self):
        rule = StructureStopRule()
        ctx = _ctx_long(
            low=3745.0, invalidation_price=3750.0, bi_begin_price=3730.0, zs_low=3760.0,
        )
        result = rule.check(ctx)
        assert result.exit_price == 3760.0

    def test_short_stop_triggered(self):
        rule = StructureStopRule()
        ctx = _ctx_short(high=3855.0, invalidation_price=3850.0, bi_begin_price=3860.0)
        result = rule.check(ctx)
        assert result.exit_price == 3850.0

    def test_grade_tighten(self):
        rule = StructureStopRule(grade_tighten=True)
        ctx = _ctx_long(low=3770.0, invalidation_price=3750.0, entry_grade="weak")
        result = rule.check(ctx)
        assert result.exit_price == 3775.0


# ══════════════════════════════════════════════════════════
# FixedStopRule
# ══════════════════════════════════════════════════════════

class TestFixedStopRule:
    def test_long_stop_triggered(self):
        rule = FixedStopRule(stop_points=180.0, grade_adjust=False)
        ctx = _ctx_long(low=3615.0, entry_price=3800.0)
        result = rule.check(ctx)
        assert result is not None
        assert result.exit_price == 3620.0

    def test_short_stop_triggered(self):
        rule = FixedStopRule(stop_points=180.0, grade_adjust=False)
        ctx = _ctx_short(high=3985.0, entry_price=3800.0)
        result = rule.check(ctx)
        assert result.exit_price == 3980.0

    def test_grade_adjust_standard(self):
        rule = FixedStopRule(stop_points=200.0, grade_adjust=True)
        ctx = _ctx_long(low=3635.0, entry_price=3800.0, entry_grade="standard")
        assert rule.check(ctx).exit_price == 3640.0


# ══════════════════════════════════════════════════════════
# TrailingStopRule
# ══════════════════════════════════════════════════════════

class TestTrailingStopRule:
    def test_no_trigger_below_threshold(self):
        rule = TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5)
        ctx = _ctx_long(mfe=400.0, current_move=350.0)
        assert rule.check(ctx) is None

    def test_exit_when_giveback_exceeds(self):
        rule = TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5)
        ctx = _ctx_long(mfe=600.0, current_move=200.0)
        assert rule.check(ctx) is not None


# ══════════════════════════════════════════════════════════
# TimeStopRule
# ══════════════════════════════════════════════════════════

class TestTimeStopRule:
    def test_not_triggered_before_max(self):
        rule = TimeStopRule(max_bars=100)
        ctx = _ctx_long(bars_since_entry=50)
        assert rule.check(ctx) is None

    def test_triggered_at_boundary(self):
        rule = TimeStopRule(max_bars=100)
        ctx = _ctx_long(bars_since_entry=100)
        assert rule.check(ctx) is not None


# ══════════════════════════════════════════════════════════
# OppositeSignalRule
# ══════════════════════════════════════════════════════════

class TestOppositeSignalRule:
    def test_triggered(self):
        rule = OppositeSignalRule()
        ctx = _ctx_long(opposite_signal_triggered=True)
        assert rule.check(ctx) is not None

    def test_no_signal(self):
        rule = OppositeSignalRule()
        ctx = _ctx_long(opposite_signal_triggered=False)
        assert rule.check(ctx) is None


# ══════════════════════════════════════════════════════════
# MACDCrossRule (V2: boolean-driven only, no fallback)
# ══════════════════════════════════════════════════════════

class TestMACDCrossRule:
    def test_long_death_cross(self):
        rule = MACDCrossRule()
        ctx = _ctx_long(macd_cross_down=True)
        assert rule.check(ctx) is not None

    def test_long_no_cross(self):
        rule = MACDCrossRule()
        ctx = _ctx_long(macd_cross_down=False, macd_cross_up=False)
        assert rule.check(ctx) is None

    def test_short_golden_cross(self):
        rule = MACDCrossRule()
        ctx = _ctx_short(macd_cross_up=True)
        assert rule.check(ctx) is not None


# ══════════════════════════════════════════════════════════
# ChanDivergenceExitRule (V2: TIGHTEN_STOP / CLOSE_ALL)
# ══════════════════════════════════════════════════════════

class TestChanDivergenceExitRule:
    def test_no_chan_snapshot(self):
        rule = ChanDivergenceExitRule(require_second_level_confirm=False)
        ctx = _ctx_long()
        assert rule.check(ctx) is None

    def test_no_areas(self):
        rule = ChanDivergenceExitRule(require_second_level_confirm=False)
        ctx = _ctx_long(chan=_chan_snapshot(is_new_high_low=True))
        assert rule.check(ctx) is None

    def test_not_new_high_low(self):
        rule = ChanDivergenceExitRule(require_second_level_confirm=False)
        ctx = _ctx_long(chan=_chan_snapshot(
            is_new_high_low=False, macd_areas=[100.0, 80.0],
        ))
        assert rule.check(ctx) is None

    def test_divergence_without_second_confirm_closes_all(self):
        """主级别背驰 + 不需要次级别确认 → 全平。"""
        rule = ChanDivergenceExitRule(require_second_level_confirm=False)
        ctx = _ctx_long(chan=_chan_snapshot(
            is_new_high_low=True, macd_areas=[100.0, 60.0],
        ))
        result = rule.check(ctx)
        assert result is not None
        assert result.reason_code == "chan_divergence"

    def test_divergence_warning_with_second_level_unconfirmed(self):
        """主级别背驰 + 次级别未确认 → 收紧止损 (不触发 check() 的 ExitSignal)。"""
        rule = ChanDivergenceExitRule(require_second_level_confirm=True)
        ctx = _ctx_long(
            chan=_chan_snapshot(
                is_new_high_low=True,
                macd_areas=[100.0, 60.0],
                adjacent_bidong=[30.0, 50.0],  # 次级别力度未减弱
            ),
            zs_low=3760.0,
        )
        # check() 不应返回 ExitSignal（因为 directive 是 TIGHTEN_STOP, 非 CLOSE_ALL）
        result = rule.check(ctx)
        assert result is None

        # check_with_directive 应返回 TIGHTEN_STOP
        d = rule.check_with_directive(ctx)
        assert d.action == ExitAction.TIGHTEN_STOP
        assert d.target_stop_price is not None

    def test_divergence_with_sub_confirm_closes_all(self):
        """主级别 + 次级别都背驰 → 全平。"""
        rule = ChanDivergenceExitRule(require_second_level_confirm=True)
        ctx = _ctx_long(chan=_chan_snapshot(
            is_new_high_low=True,
            macd_areas=[100.0, 60.0],
            adjacent_bidong=[50.0, 30.0],  # 次级别力度也减弱
        ))
        assert rule.check(ctx) is not None

    def test_short_divergence(self):
        rule = ChanDivergenceExitRule(require_second_level_confirm=False)
        ctx = _ctx_short(chan=_chan_snapshot(
            is_new_high_low=True,
            macd_areas=[-30.0, -120.0, -60.0],  # | -60 | < | -120 |
        ))
        assert rule.check(ctx) is not None


# ══════════════════════════════════════════════════════════
# ChanSegmentCompleteExitRule (V2: REDUCE / CLOSE_ALL)
# ══════════════════════════════════════════════════════════

class TestChanSegmentCompleteExitRule:
    def test_no_chan(self):
        rule = ChanSegmentCompleteExitRule()
        ctx = _ctx_long()
        assert rule.check(ctx) is None

    def test_no_segment_complete(self):
        rule = ChanSegmentCompleteExitRule()
        ctx = _ctx_long(chan=_chan_snapshot(segment_complete=False))
        assert rule.check(ctx) is None

    def test_warning_without_new_bi_reduces(self):
        """线段终结但无反向笔 → 减仓 (不触发 check() 全平)。"""
        rule = ChanSegmentCompleteExitRule(require_new_bi=True)
        ctx = _ctx_long(chan=_chan_snapshot(
            segment_complete=True,
            adjacent_bidong=[20.0, 30.0],  # 全部上涨，无反向笔
        ))
        result = rule.check(ctx)
        assert result is None  # check() 不触发全平

        d = rule.check_with_directive(ctx)
        assert d.action == ExitAction.REDUCE

    def test_full_exit_with_new_bi(self):
        """线段终结 + 反向笔 → 全平。"""
        rule = ChanSegmentCompleteExitRule(require_new_bi=True)
        ctx = _ctx_long(chan=_chan_snapshot(
            segment_complete=True,
            adjacent_bidong=[20.0, 30.0, -15.0],
            prev_low=3790.0,
        ), low=3780.0)
        assert rule.check(ctx) is not None

    def test_min_new_bi_amplitude_blocks(self):
        rule = ChanSegmentCompleteExitRule(require_new_bi=True, min_new_bi_amplitude=20.0)
        ctx = _ctx_long(chan=_chan_snapshot(
            segment_complete=True,
            adjacent_bidong=[20.0, 30.0, -10.0],
        ))
        d = rule.check_with_directive(ctx)
        assert d.action == ExitAction.REDUCE  # 反向笔太小，仅减仓


# ══════════════════════════════════════════════════════════
# ChanSmallTurnExitRule (V2: priority=5, always CLOSE_ALL)
# ══════════════════════════════════════════════════════════

class TestChanSmallTurnExitRule:
    def test_no_chan(self):
        rule = ChanSmallTurnExitRule()
        ctx = _ctx_long()
        assert rule.check(ctx) is None

    def test_normal_amplitude(self):
        rule = ChanSmallTurnExitRule(require_fractal_break=False)
        ctx = _ctx_long(chan=_chan_snapshot(
            bar_amplitude=10.0, avg_amplitude_20=10.0,
        ), close=3790.0, open=3800.0)
        assert rule.check(ctx) is None

    def test_bullish_bar_for_long_not_triggered(self):
        rule = ChanSmallTurnExitRule(require_fractal_break=False)
        ctx = _ctx_long(chan=_chan_snapshot(
            bar_amplitude=30.0, avg_amplitude_20=10.0,
        ), close=3830.0, open=3800.0)
        assert rule.check(ctx) is None

    def test_bearish_bar_without_fractal_triggers(self):
        rule = ChanSmallTurnExitRule(require_fractal_break=False)
        ctx = _ctx_long(chan=_chan_snapshot(
            bar_amplitude=30.0, avg_amplitude_20=10.0,
        ), close=3770.0, open=3800.0)
        assert rule.check(ctx) is not None

    def test_bearish_bar_fractal_not_broken(self):
        rule = ChanSmallTurnExitRule(require_fractal_break=True)
        ctx = _ctx_long(chan=_chan_snapshot(
            bar_amplitude=30.0, avg_amplitude_20=10.0,
            fractal_break_price=3750.0,
        ), close=3770.0, open=3800.0, low=3760.0)
        assert rule.check(ctx) is None

    def test_bearish_bar_fractal_broken_triggers(self):
        rule = ChanSmallTurnExitRule(require_fractal_break=True)
        ctx = _ctx_long(chan=_chan_snapshot(
            bar_amplitude=30.0, avg_amplitude_20=10.0,
            fractal_break_price=3750.0,
        ), close=3770.0, open=3800.0, low=3740.0)
        assert rule.check(ctx) is not None

    def test_priority(self):
        rule = ChanSmallTurnExitRule()
        assert rule.priority == 5
        assert rule.category == "chan_structure"


# ══════════════════════════════════════════════════════════
# ExitManager V2
# ══════════════════════════════════════════════════════════

class TestExitManagerV2:
    def test_strict_mode_raises_on_exception(self):
        class BoomRule(ExitRule):
            def check(self, ctx):
                raise RuntimeError("boom")

        manager = ExitManager([BoomRule()], strict=True)
        manager.on_entry(direction=SignalDirection.LONG, entry_price=3800.0)
        with pytest.raises(RuntimeError):
            manager.check(**_check())

    def test_non_strict_collects_errors(self):
        class BoomRule(ExitRule):
            @property
            def rule_id(self):
                return "BoomRule"

            def check(self, ctx):
                raise RuntimeError("boom")

        manager = ExitManager([BoomRule(), TimeStopRule(max_bars=1)], strict=False)
        manager.on_entry(direction=SignalDirection.LONG, entry_price=3800.0)
        result = manager.check(**_check())
        assert result is not None
        assert result.reason_code == "time_stop"
        assert "BoomRule" in manager.error_summary()

    def test_category_sorting_chan_before_profit(self):
        divert = ChanDivergenceExitRule(require_second_level_confirm=False)
        trail = TrailingStopRule(trigger_points=100.0, giveback_ratio=0.5)
        manager = ExitManager([trail, divert])
        manager.on_entry(direction=SignalDirection.LONG, entry_price=3800.0)
        result = manager.check(
            chan_snapshot=_chan_snapshot(is_new_high_low=True, macd_areas=[100.0, 60.0]),
            **_check(),
        )
        assert result is not None
        assert result.reason_code == "chan_divergence"

    def test_small_turn_before_divergence(self):
        """小转大(priority=5)应在背驰(priority=10)之前触发。"""
        small = ChanSmallTurnExitRule(require_fractal_break=False)
        divert = ChanDivergenceExitRule(require_second_level_confirm=False)
        manager = ExitManager([divert, small])
        manager.on_entry(direction=SignalDirection.LONG, entry_price=3800.0)
        result = manager.check(
            chan_snapshot=_chan_snapshot(
                is_new_high_low=True, macd_areas=[100.0, 60.0],
                bar_amplitude=30.0, avg_amplitude_20=10.0,
            ),
            bar_end_time=_NOW,
            open=3800.0,
            high=3810.0,
            low=3740.0,
            close=3770.0,
        )
        assert result is not None
        assert result.reason_code == "chan_divergence"  # small turn only triggers(CLOSE_ALL) when fractal break, which we didn't set.

    def test_chan_snapshot_passed_through(self):
        manager = ExitManager([ChanDivergenceExitRule(require_second_level_confirm=False)])
        manager.on_entry(direction=SignalDirection.LONG, entry_price=3800.0)
        result = manager.check(
            chan_snapshot=_chan_snapshot(is_new_high_low=True, macd_areas=[100.0, 60.0]),
            **_check(),
        )
        assert result is not None
        assert result.reason_code == "chan_divergence"
