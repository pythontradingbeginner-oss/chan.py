from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext
from vnpy_chan.oms_reconciliation import (
    reconcile,
    oms_active_order_ids,
    oms_net_position,
    signed_volume,
)
from vnpy_chan.runtime_state import (
    RuntimeStatePackage,
    RuntimeStateVersionError,
    STATE_VERSION,
)


def _position(direction: str, volume: int):
    return SimpleNamespace(
        symbol="rb2610", exchange="SHFE", gateway_name="CTP",
        direction=direction, volume=volume,
    )


def _order(vt_orderid: str, status: str):
    return SimpleNamespace(
        vt_orderid=vt_orderid, orderid=vt_orderid, symbol="rb2610",
        exchange="SHFE", gateway_name="CTP", status=status,
    )


def _context() -> PositionContext:
    return PositionContext(
        direction=SignalDirection.LONG,
        entry_price=3500,
        entry_time="2026-08-04 09:01",
        entry_bar=10,
        volume=1,
    )


def test_oms_net_position_sums_long_minus_short() -> None:
    positions = [
        _position("LONG", 2),
        _position("SHORT", 1),
        SimpleNamespace(symbol="rb2505", direction="LONG", volume=9),
    ]
    assert oms_net_position(positions, "rb2610") == 1


def test_oms_active_order_ids_filters_terminal() -> None:
    orders = [
        _order("A", "SUBMITTING"),
        _order("B", "ALLTRADED"),
        _order("C", "REJECTED"),
        _order("D", "PARTTRADED"),
    ]
    assert oms_active_order_ids(orders) == ("A", "D")


def test_signed_volume_long_positive_short_negative() -> None:
    assert signed_volume(_context()) == 1
    short = _context().__class__(
        direction=SignalDirection.SHORT,
        entry_price=3500,
        entry_time="x",
        entry_bar=1,
        volume=2,
    )
    assert signed_volume(short) == -2
    assert signed_volume(None) == 0


def test_aligned_when_oms_matches_strategy() -> None:
    result = reconcile(
        oms_positions=[_position("LONG", 1)],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=_context(),
        strategy_active_order_ids=(),
    )
    assert result.aligned
    assert result.allow_open
    assert result.allow_close


def test_position_mismatch_triggers_recovery() -> None:
    result = reconcile(
        oms_positions=[_position("LONG", 1)],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert not result.allow_open
    assert result.allow_close
    assert any("unattributed_position" in r for r in result.reasons)


def test_active_order_mismatch_triggers_recovery() -> None:
    result = reconcile(
        oms_positions=[],
        oms_orders=[_order("SIM.1", "SUBMITTING")],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert any("active_order_mismatch" in r for r in result.reasons)


def test_flat_aligned_with_no_context() -> None:
    result = reconcile(
        oms_positions=[],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.aligned
    assert result.allow_open


def test_unattributed_position_never_autoclaimed() -> None:
    result = reconcile(
        oms_positions=[_position("LONG", 1)],
        oms_orders=[],
        symbol="rb2610",
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert result.oms_net_position == 1
    assert result.strategy_net_position == 0
    assert any("unattributed_position" in r for r in result.reasons)


def test_runtime_state_package_roundtrip() -> None:
    package = RuntimeStatePackage(
        strategy_name="ChanBspStrategy",
        vt_symbol="rb2610.SHFE",
        config_sha256="abc123",
        position={"volume": 1},
        risk={"realized_points": -50},
        orders={"active": []},
        decision_ids=["d1", "d2"],
        trade_ids=["t1"],
    )
    restored = RuntimeStatePackage.from_json(
        package.to_json(),
        expected_strategy_name="ChanBspStrategy",
        expected_vt_symbol="rb2610.SHFE",
        expected_config_sha256="abc123",
    )
    assert restored.position == {"volume": 1}
    assert restored.risk == {"realized_points": -50}
    assert restored.decision_ids == ["d1", "d2"]
    assert restored.trade_ids == ["t1"]


def test_runtime_state_package_rejects_wrong_version() -> None:
    payload = {
        "state_version": STATE_VERSION + 1,
        "strategy_name": "ChanBspStrategy",
        "persisted_at": "",
    }
    with pytest.raises(RuntimeStateVersionError, match="state_version_mismatch"):
        RuntimeStatePackage.from_dict(payload)


def test_runtime_state_package_rejects_wrong_strategy_name() -> None:
    package = RuntimeStatePackage(strategy_name="OtherStrategy")
    with pytest.raises(RuntimeStateVersionError, match="strategy_name_mismatch"):
        RuntimeStatePackage.from_json(
            package.to_json(),
            expected_strategy_name="ChanBspStrategy",
        )


def _strategy(recovery_required: bool) -> object:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.shadow_mode = True
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.vt_symbol = "rb2610.SHFE"
    strategy._recovery_required = recovery_required
    strategy.recovery_reasons = (
        "position_mismatch:oms=1 strategy=0" if recovery_required else ""
    )
    strategy.write_log = lambda message: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="shadow_mode_enabled",
    )
    strategy._order_state = __import__(
        "vnpy_chan.order_state", fromlist=["CtaOrderStatusMachine"]
    ).CtaOrderStatusMachine()
    return strategy


def test_recovery_blocks_open_even_in_shadow() -> None:
    strategy = _strategy(recovery_required=True)
    called = []

    def sender():
        called.append(True)
        return ["SIM.1"]

    result = strategy._send_live_or_shadow(
        "open_long",
        sender,
        price=3500,
        volume=1,
        closing=False,
        reason="entry",
    )

    assert result == []
    assert not called
    assert strategy.order_status == "recovery_required"


def test_recovery_allows_close() -> None:
    strategy = _strategy(recovery_required=True)
    called = []

    def sender():
        called.append(True)
        return ["SIM.1"]

    result = strategy._send_live_or_shadow(
        "close",
        sender,
        price=3500,
        volume=1,
        closing=True,
        reason="stop",
    )

    assert result == ["SIM.1"]
    assert called


def test_oms_query_error_forces_recovery() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.cta_engine = SimpleNamespace(
        main_engine=SimpleNamespace(
            get_engine=lambda name: (_ for _ in ()).throw(RuntimeError("oms down"))
        )
    )
    strategy._order_state = __import__(
        "vnpy_chan.order_state", fromlist=["CtaOrderStatusMachine"]
    ).CtaOrderStatusMachine()
    strategy._position_context = None
    strategy._recovery_required = False
    strategy.recovery_status = "not_checked"
    strategy.recovery_reasons = ""
    strategy.write_log = lambda message: None

    strategy._check_oms_reconciliation()

    assert strategy.recovery_status == "RECOVERY_REQUIRED"
    assert strategy._recovery_required is True


def test_oms_reconciliation_wiring() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
    from vnpy_chan.order_state import CtaOrderStatusMachine

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.pos = 1  # engine-derived net position matches PositionContext
    # P7-R10: _check_oms_reconciliation now resolves gateway via get_contract
    contract = SimpleNamespace(gateway_name="CTP")
    strategy.cta_engine = SimpleNamespace(
        main_engine=SimpleNamespace(
            get_engine=lambda name: SimpleNamespace(
                get_all_positions=lambda: [_position("LONG", 1)],
                get_all_active_orders=lambda: [],
            ),
            get_contract=lambda vt_symbol: contract,
        )
    )
    strategy._order_state = CtaOrderStatusMachine()
    strategy._position_context = _context()
    strategy._recovery_required = False
    strategy.recovery_status = "not_checked"
    strategy.recovery_reasons = ""
    strategy.write_log = lambda message: None

    strategy._check_oms_reconciliation()

    assert strategy.recovery_status == "ALIGNED"
    assert strategy._recovery_required is False

