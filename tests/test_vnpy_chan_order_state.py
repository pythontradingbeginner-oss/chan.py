from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from chan_futures.risk import CloseFillRecord, RiskConfig, RiskManager
from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.production_guard import evaluate_production_gate
from vnpy_chan.production_guard import RiskEngineLiveStatus
from vnpy.trader.constant import Offset


class _ExitManagerCapture:
    def __init__(self) -> None:
        self.context = None

    def on_position_opened(self, context) -> None:
        self.context = context

    def on_position_updated(self, context) -> None:
        self.context = context

    def on_close(self) -> None:
        self.context = None


def test_cta_order_state_tracks_partial_and_terminal_updates() -> None:
    machine = CtaOrderStatusMachine()
    machine.submit(["SIM.1"], role="open", price=3500, volume=2, reason="entry")

    machine.on_trade(SimpleNamespace(vt_orderid="SIM.1", volume=1))
    assert machine.active["SIM.1"].status == "PARTTRADED"
    assert machine.summary == "PARTTRADED:1"

    machine.on_order(
        SimpleNamespace(
            vt_orderid="SIM.1",
            price=3500,
            volume=2,
            traded=2,
            status=SimpleNamespace(name="ALLTRADED"),
            is_active=lambda: False,
        )
    )
    assert machine.summary == "idle"
    assert machine.history[-1].status == "ALLTRADED"


def test_cta_order_state_archives_terminal_stop_order() -> None:
    machine = CtaOrderStatusMachine()
    machine.on_stop_order(
        SimpleNamespace(
            stop_orderid="STOP.1",
            price=3490,
            volume=1,
            status=SimpleNamespace(name="CANCELLED"),
        )
    )

    assert machine.summary == "idle"
    assert machine.history[-1].vt_orderid == "STOP.1"


def test_strategy_rejected_open_order_clears_pending_entry() -> None:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(["SIM.1"], role="open", price=3500, volume=1)
    strategy._pending_entry = object()
    strategy.put_event = lambda: None
    strategy._persist_runtime_state = lambda: None

    strategy.on_order(
        SimpleNamespace(
            vt_orderid="SIM.1",
            price=3500,
            volume=1,
            traded=0,
            status=SimpleNamespace(name="REJECTED"),
            is_active=lambda: False,
        )
    )

    assert strategy._pending_entry is None
    assert strategy.order_status == "idle"


def test_pending_order_count_blocks_live_production_gate(tmp_path) -> None:
    enabled = tmp_path / "risk_manager_setting.json"
    enabled.write_text(
        '{"活动委托检查":{"active":true},"缠论开仓守卫":{"active":true}}',
        encoding="utf-8",
    )
    config = SimpleNamespace(
        production=SimpleNamespace(enabled=True),
        risk=SimpleNamespace(
            max_loss_points=600,
            daily_loss_limit=None,
            max_consecutive_losses=None,
            max_drawdown_pct=None,
        ),
        exits=[object()],
    )

    result = evaluate_production_gate(
        config=config,
        kl_window=15,
        production_ready=True,
        operator_confirmed=True,
        shadow_mode=False,
        forward_confirmed=True,
        risk_manager_confirmed=True,
        risk_manager_setting_path=enabled,
        pending_order_count=1,
        live_risk_status=RiskEngineLiveStatus(
            risk_manager_app_loaded=True,
            risk_engine_present=True,
            send_order_patched=True,
            loaded_rules=("活动委托检查", "缠论开仓守卫"),
            active_rules=("活动委托检查", "缠论开仓守卫"),
        ),
    )

    assert not result.ready
    assert result.reasons == ("pending_order_recovery_required:1",)


def test_strategy_restores_position_risk_and_order_state() -> None:
    source = ChanBspStrategy.__new__(ChanBspStrategy)
    source._order_state = CtaOrderStatusMachine()
    source._order_state.submit(["SIM.1"], role="open", price=3500, volume=1)
    source._risk = RiskManager(RiskConfig(daily_loss_limit=100))
    source._risk.on_fill(pnl_points=-25, fill_time="2025-01-02")
    source._position_context = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500,
        entry_time="2025-01-02 10:00:00",
        entry_bar=10,
        volume=1,
        execution_stop_price=3450,
        setup_invalidation_price=3440,
    )
    source._persist_runtime_state()

    target = ChanBspStrategy.__new__(ChanBspStrategy)
    target.order_state_json = source.order_state_json
    target.risk_state_json = source.risk_state_json
    target.position_state_json = source.position_state_json
    target._order_state = CtaOrderStatusMachine()
    target._risk = RiskManager(RiskConfig(daily_loss_limit=100))
    target._exit_manager = _ExitManagerCapture()
    target._runtime_state_restored = False
    target.order_status = "idle"
    target.risk_status = "unknown"
    target.write_log = lambda message: None

    target._restore_runtime_state()

    assert target.order_status == "SUBMITTING:1"
    assert target._risk.get_state()["daily_realized"] == -25
    assert target.position_context.execution_stop_price == 3450
    assert target._exit_manager.context is target.position_context


def test_strategy_restores_partial_close_aggregator_and_fill_ids() -> None:
    source = ChanBspStrategy.__new__(ChanBspStrategy)
    source._order_state = CtaOrderStatusMachine()
    source._risk = RiskManager(RiskConfig(daily_loss_limit=100))
    source._position_context = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500,
        entry_time="2025-01-02 10:00:00",
        entry_bar=10,
        volume=1,
        position_id="position-partial",
    )
    source._risk.record_close_fill(
        CloseFillRecord(
            fill_id="fill-partial-1",
            order_id="order-partial",
            position_id="position-partial",
            pnl_raw_points=-5,
            risk_session_key="2025-01-02",
            observed_at="2025-01-02 10:30:00",
            source="cta_fill",
        ),
        remaining_quantity=1,
    )
    source._persist_runtime_state()

    target = ChanBspStrategy.__new__(ChanBspStrategy)
    target.order_state_json = source.order_state_json
    target.risk_state_json = source.risk_state_json
    target.position_state_json = source.position_state_json
    target._order_state = CtaOrderStatusMachine()
    target._risk = RiskManager(RiskConfig(daily_loss_limit=100))
    target._exit_manager = _ExitManagerCapture()
    target._runtime_state_restored = False
    target.order_status = "idle"
    target.risk_status = "unknown"
    target.write_log = lambda message: None

    target._restore_runtime_state()

    restored = target._risk.get_state()
    assert target.position_context.position_id == "position-partial"
    assert restored["processed_close_fill_ids"] == ["fill-partial-1"]
    assert restored["active_position_aggregators"]["position-partial"][
        "remaining_quantity"
    ] == 1


def test_cta_partial_close_finalizes_once_before_reversal_open_checkpoint() -> None:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._order_state.submit(
        ["SIM.CLOSE"], role="close", price=99, volume=2, reason="reverse"
    )
    strategy._risk = RiskManager(RiskConfig(max_consecutive_losses=2))
    strategy._position_context = PositionContext(
        direction=SignalDirection.LONG,
        entry_price=100,
        entry_time="2026-08-13 09:00:00",
        entry_bar=1,
        volume=2,
        position_id="position-cta-partial",
    )
    strategy._config = SimpleNamespace(
        execution=SimpleNamespace(fee_points=0.0),
        risk=SimpleNamespace(max_drawdown_pct=None),
    )
    strategy._risk_session_resolver = SimpleNamespace(
        resolve=lambda observed_at, source: SimpleNamespace(
            session_key="2026-08-13",
            observed_at=observed_at,
            source=source,
        )
    )
    strategy._account_equity_observation = lambda: (
        None,
        "config_fallback",
        "oms_all_accounts",
    )
    strategy._exit_manager = _ExitManagerCapture()
    strategy._pending_entry = None
    strategy._pending_reversal = SimpleNamespace(intent="reverse-intent")
    strategy.total_pnl = 0.0
    strategy.total_trades = 0
    strategy._trade_pnl_batch = []
    strategy.exit_reason = "strategy_reverse"
    strategy.put_event = lambda: None
    strategy.write_log = lambda message: None

    events: list[object] = []
    strategy.trading = True
    strategy._persist_runtime_state = lambda: events.append("build")
    strategy.sync_data = lambda: events.append("sync")
    strategy._approve_and_submit_open_intent = (
        lambda intent, before_position: events.append(
            (
                "open",
                intent,
                strategy._risk.get_state()["true_consecutive_losses"],
            )
        )
    )

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.CLOSE,
            vt_tradeid="SIM.FILL-1",
            vt_orderid="SIM.CLOSE",
            volume=1,
            price=99,
            datetime=datetime(2026, 8, 13, 10, 0),
        )
    )

    partial_state = strategy._risk.get_state()
    assert strategy.position_context.volume == 1
    assert partial_state["true_consecutive_losses"] == 0
    assert partial_state["active_position_aggregators"][
        "position-cta-partial"
    ]["remaining_quantity"] == 1
    assert not any(isinstance(item, tuple) and item[0] == "open" for item in events)

    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.CLOSE,
            vt_tradeid="SIM.FILL-2",
            vt_orderid="SIM.CLOSE",
            volume=1,
            price=99,
            datetime=datetime(2026, 8, 13, 10, 1),
        )
    )

    final_state = strategy._risk.get_state()
    open_event = ("open", "reverse-intent", 1)
    assert strategy.position_context is None
    assert final_state["true_consecutive_losses"] == 1
    assert final_state["finalized_position_ids"] == ["position-cta-partial"]
    assert events.index("sync") < events.index(open_event)

    state_after_final = strategy._risk.get_state()
    strategy.on_trade(
        SimpleNamespace(
            offset=Offset.CLOSE,
            vt_tradeid="SIM.FILL-2",
            vt_orderid="SIM.CLOSE",
            volume=1,
            price=99,
            datetime=datetime(2026, 8, 13, 10, 1),
        )
    )
    assert strategy._risk.get_state() == state_after_final


def test_cta_reversal_open_is_reapproved_after_loss_finalization() -> None:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._risk = RiskManager(RiskConfig(max_consecutive_losses=1))
    strategy._risk.on_fill(pnl_points=-1)
    submitted: list[object] = []
    logs: list[str] = []
    strategy._account_equity_observation = lambda: (
        None,
        "config_fallback",
        "oms_all_accounts",
    )
    strategy._persist_runtime_state = lambda: None
    strategy._submit_open_intent = submitted.append
    strategy.write_log = logs.append
    intent = SimpleNamespace(
        signal=SimpleNamespace(target_position=-1),
        with_signal=lambda signal: SimpleNamespace(signal=signal),
    )

    strategy._approve_and_submit_open_intent(intent, before_position=0)

    assert submitted == []
    assert strategy.risk_status.startswith("max_consecutive_losses")
    assert any(message.startswith("Risk rejected:") for message in logs)


def test_risk_manager_restored_peak_equity_blocks_drawdown() -> None:
    risk = RiskManager(RiskConfig(max_drawdown_pct=0.1))
    risk.load_state({"peak_equity": 100000, "realized_points": 0})

    decision = risk.approve(
        SimpleNamespace(target_position=1),
        current_equity=89000,
    )

    assert not decision.approved
    assert decision.reason.startswith("max_drawdown")


def test_risk_manager_tracks_initial_and_mark_to_market_equity_peak() -> None:
    risk = RiskManager(
        RiskConfig(max_drawdown_pct=0.031),
        initial_equity=100000,
    )
    risk.observe_equity(102000)

    decision = risk.approve(
        SimpleNamespace(target_position=1),
        current_equity=98500,
    )

    assert risk.get_state()["peak_equity"] == 102000
    assert not decision.approved
    assert decision.reason.startswith("max_drawdown: 3.43%")


def test_risk_manager_restore_keeps_daily_loss_on_same_day() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100))
    risk.load_state({"current_date": "2025-01-02", "daily_realized": -40})
    risk.on_fill(pnl_points=-10, fill_time="2025-01-02")

    assert risk.get_state()["daily_realized"] == -50
