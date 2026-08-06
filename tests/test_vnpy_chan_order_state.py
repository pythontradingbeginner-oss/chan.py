from __future__ import annotations

from types import SimpleNamespace

from chan_futures.risk import RiskConfig, RiskManager
from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.production_guard import evaluate_production_gate
from vnpy_chan.production_guard import RiskEngineLiveStatus


class _ExitManagerCapture:
    def __init__(self) -> None:
        self.context = None

    def on_position_opened(self, context) -> None:
        self.context = context


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


def test_risk_manager_restored_peak_equity_blocks_drawdown() -> None:
    risk = RiskManager(RiskConfig(max_drawdown_pct=0.1))
    risk.load_state({"peak_equity": 100000, "realized_points": 0})

    decision = risk.approve(
        SimpleNamespace(target_position=1),
        current_equity=89000,
    )

    assert not decision.approved
    assert decision.reason.startswith("max_drawdown")


def test_risk_manager_restore_keeps_daily_loss_on_same_day() -> None:
    risk = RiskManager(RiskConfig(daily_loss_limit=100))
    risk.load_state({"current_date": "2025-01-02", "daily_realized": -40})
    risk.on_fill(pnl_points=-10, fill_time="2025-01-02")

    assert risk.get_state()["daily_realized"] == -50
