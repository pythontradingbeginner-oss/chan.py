from collections import defaultdict
from datetime import datetime

import pandas as pd

from chan_futures.backtest import _calculate_atr, _new_chan, _resolve_kl_type
from chan_futures.config_loader import load_config, make_runtime_decision_kernel
from chan_futures.feed import prepare_ohlc_frame, row_to_klu
from chan_futures.parity import compare_runtime_traces
from signal_core.models import SignalDirection
from strategy_policy.exit_rules import ExitManager
from strategy_policy.position import PositionContext


def test_position_update_preserves_exit_tracking_state() -> None:
    manager = ExitManager([])
    position = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500.0,
        entry_time=datetime(2024, 1, 1, 9, 0),
        entry_bar=10,
        volume=3,
        execution_stop_price=3480.0,
    )
    manager.on_position_opened(position)
    manager.check(
        bar_end_time=datetime(2024, 1, 1, 9, 15),
        open=3500.0,
        high=3520.0,
        low=3490.0,
        close=3510.0,
    )

    reduced = position.reduce_volume(1)
    assert reduced is not None
    manager.on_position_updated(reduced)

    assert manager.bars_since_entry == 1
    assert manager.position_context is reduced
    assert manager.position_context.volume == 2


def test_same_rb_bars_produce_identical_three_runtime_decision_traces() -> None:
    config = load_config("configs/rb_15m_qingpai_strict.yaml")
    bars = prepare_ohlc_frame(
        pd.read_parquet(config.data_path).iloc[:1500].reset_index(drop=True)
    )
    kl_type = _resolve_kl_type(config.kl_type)
    atr_values = _calculate_atr(bars, config.sizing.atr_period)
    lanes = {
        name: (
            _new_chan(config, kl_type),
            make_runtime_decision_kernel(config, symbol="RB", timeframe="15m"),
        )
        for name in ("backtest", "vnpy_cta", "live")
    }
    positions = {name: 0 for name in lanes}

    for row_number, row in bars.iterrows():
        timestamp = row["datetime"]
        active_symbol = row.get("active_symbol")
        active_symbol_str = str(active_symbol) if pd.notna(active_symbol) else None
        atr = atr_values.iloc[row_number]
        atr_value = None if pd.isna(atr) else float(atr)
        for name, (chan, kernel) in lanes.items():
            klu = row_to_klu(row, kl_type=kl_type)
            chan.trigger_load({kl_type: [klu]})
            intent = kernel.evaluate_bar(
                chan=chan,
                current_position=positions[name],
                price=float(row["close"]),
                timestamp=timestamp,
                active_symbol=active_symbol_str,
                lv_idx=0,
                account_equity=config.sizing.capital,
                atr=atr_value,
            )
            if intent is not None and intent.accepted:
                positions[name] = intent.signal.target_position

    traces = {name: kernel.decision_trace for name, (_, kernel) in lanes.items()}
    decomposition_traces = {
        name: kernel.decomposition_transitions for name, (_, kernel) in lanes.items()
    }
    report = compare_runtime_traces(traces)
    baseline = traces["backtest"]

    assert report.trace_lengths["backtest"] > 0
    assert any(record.accepted for record in baseline)
    assert any(not record.accepted for record in baseline)
    assert any(record.position_size > 0 for record in baseline if record.accepted)
    assert all(
        record.setup_invalidation_price is not None
        and record.execution_stop_price is not None
        for record in baseline
        if record.accepted
    )
    assert report.trace_lengths == {
        "backtest": report.trace_lengths["backtest"],
        "vnpy_cta": report.trace_lengths["backtest"],
        "live": report.trace_lengths["backtest"],
    }
    assert len(set(positions.values())) == 1
    assert decomposition_traces["backtest"]
    assert decomposition_traces["backtest"] == decomposition_traces["vnpy_cta"]
    assert decomposition_traces["backtest"] == decomposition_traces["live"]
    assert [transition.sequence for transition in decomposition_traces["backtest"]] == list(
        range(len(decomposition_traces["backtest"]))
    )
    assert len({transition.transition_id for transition in decomposition_traces["backtest"]}) == len(
        decomposition_traces["backtest"]
    )
    by_decomposition = defaultdict(list)
    for transition in decomposition_traces["backtest"]:
        by_decomposition[transition.decomposition_id].append(transition)
    assert any(
        transition.kind.value == "close"
        for transition in decomposition_traces["backtest"]
    )
    for transitions in by_decomposition.values():
        assert [transition.revision for transition in transitions] == list(
            range(len(transitions))
        )
        if any(transition.kind.value == "close" for transition in transitions):
            assert transitions[-1].kind.value == "close"
    assert any(record.regime != "unclassified" for record in baseline)
    report.assert_consistent()
