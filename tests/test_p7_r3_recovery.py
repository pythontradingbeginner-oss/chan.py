"""P7-R3: state recovery + OMS three-way reconciliation.

Requirements under test:
  1. RuntimeStatePackage v2 carries strategy_name / vt_symbol / config SHA256.
  2. exit_state / decision_ids / trade_ids are fully restored (not just written).
  3. Reconciliation compares THREE sources: self.pos, PositionContext, OMS.
  4. Positions/orders are filtered by symbol, exchange and gateway.
  5. Other-contract active orders do not raise false alarms.
  6. OMS absent / query failure must fail closed (RECOVERY_REQUIRED).
  7. Hard recovery errors accumulate monotonically; a later ALIGNED result
     must not clear them.
  8. Active open orders only continue if a full PendingEntry can be restored.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from signal_core.models import SignalDirection
from strategy_policy.position import PositionContext
from vnpy_chan.oms_reconciliation import reconcile
from vnpy_chan.runtime_state import RuntimeStatePackage, RuntimeStateVersionError


def _pos(symbol="rb2610", exchange="SHFE", gateway="CTP", direction="LONG", volume=1):
    return SimpleNamespace(
        symbol=symbol, exchange=exchange, gateway_name=gateway,
        direction=direction, volume=volume,
    )


def _order(vt_orderid="SIM.1", symbol="rb2610", exchange="SHFE",
           gateway="CTP", status="SUBMITTING"):
    return SimpleNamespace(
        vt_orderid=vt_orderid, orderid=vt_orderid, symbol=symbol,
        exchange=exchange, gateway_name=gateway, status=status,
    )


def _context() -> PositionContext:
    return PositionContext(
        direction=SignalDirection.LONG, entry_price=3500,
        entry_time="2026-08-04 09:01", entry_bar=10, volume=1,
    )


# ── 1. v2 package identity ──

def test_state_package_v2_carries_identity_and_config_sha() -> None:
    pkg = RuntimeStatePackage(
        strategy_name="chan.py", vt_symbol="rb2610.SHFE",
        config_sha256="abc123", position={"volume": 1},
    )
    data = pkg.to_dict()
    assert data["state_version"] == 2
    assert data["strategy_name"] == "chan.py"
    assert data["vt_symbol"] == "rb2610.SHFE"
    assert data["config_sha256"] == "abc123"


def test_state_package_v2_restores_exit_decisions_trades() -> None:
    pkg = RuntimeStatePackage(
        strategy_name="chan.py", vt_symbol="rb2610.SHFE", config_sha256="abc123",
        exit_state={"bars_since_entry": 3, "mfe": 12.0, "mae": -4.0},
        decision_ids=["d1", "d2"],
        trade_ids=["t1"],
        vt_tradeids=["t1"],
    )
    restored = RuntimeStatePackage.from_json(pkg.to_json())
    assert restored.exit_state == {"bars_since_entry": 3, "mfe": 12.0, "mae": -4.0}
    assert restored.decision_ids == ["d1", "d2"]
    assert restored.trade_ids == ["t1"]
    assert restored.vt_tradeids == ["t1"]


def test_state_package_v2_rejects_config_sha_mismatch() -> None:
    pkg = RuntimeStatePackage(
        strategy_name="chan.py", vt_symbol="rb2610.SHFE", config_sha256="abc123",
    )
    with pytest.raises(RuntimeStateVersionError, match="config_sha256_mismatch"):
        RuntimeStatePackage.from_json(pkg.to_json(), expected_config_sha256="def456")


# ── 2. three-way reconciliation (self.pos vs PositionContext vs OMS) ──

def test_three_way_self_pos_matches_oms_but_context_missing() -> None:
    """self.pos == OMS but PositionContext missing -> still RECOVERY_REQUIRED."""
    result = reconcile(
        oms_positions=[_pos()],
        oms_orders=[],
        symbol="rb2610",
        self_pos=1,
        strategy_context=None,  # PositionContext missing
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert not result.allow_open
    assert any("unattributed_position" in r for r in result.reasons)


def test_three_way_context_matches_oms_but_self_pos_differs() -> None:
    """PositionContext == OMS but self.pos differs -> RECOVERY_REQUIRED."""
    result = reconcile(
        oms_positions=[_pos()],
        oms_orders=[],
        symbol="rb2610",
        self_pos=0,  # self.pos disagrees
        strategy_context=_context(),
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert any("self_pos_mismatch" in r for r in result.reasons)


def test_three_way_aligned_when_all_agree() -> None:
    result = reconcile(
        oms_positions=[_pos()],
        oms_orders=[],
        symbol="rb2610",
        self_pos=1,
        strategy_context=_context(),
        strategy_active_order_ids=(),
    )
    assert result.aligned


# ── 3. exchange / gateway filtering ──

def test_other_contract_orders_do_not_raise_false_alarm() -> None:
    """Orders on a DIFFERENT symbol must not trigger recovery for this strategy."""
    result = reconcile(
        oms_positions=[],
        oms_orders=[_order(vt_orderid="OTHER.1", symbol="rb2505")],
        symbol="rb2610",
        self_pos=0,
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.aligned


def test_same_symbol_other_gateway_order_is_filtered() -> None:
    """Orders for same symbol but different gateway must be ignored."""
    result = reconcile(
        oms_positions=[],
        oms_orders=[_order(vt_orderid="G2.1", gateway="OTHER")],
        symbol="rb2610",
        self_pos=0,
        strategy_context=None,
        strategy_active_order_ids=(),
        gateway="CTP",  # strategy's own gateway
    )
    assert result.aligned


def test_unknown_order_on_this_contract_forces_recovery() -> None:
    """An unknown active order on THIS contract is RECOVERY_REQUIRED."""
    result = reconcile(
        oms_positions=[],
        oms_orders=[_order(vt_orderid="SIM.99", symbol="rb2610")],
        symbol="rb2610",
        self_pos=0,
        strategy_context=None,
        strategy_active_order_ids=(),
    )
    assert result.recovery_required
    assert any("active_order_mismatch" in r for r in result.reasons)


# ── 4. ExitManager snapshot / restore ──

def test_exit_manager_snapshot_restore_roundtrip() -> None:
    from strategy_policy.exit_rules import ExitManager

    manager = ExitManager([])
    manager.on_position_opened(_context())
    manager._tracking.bars_since_entry = 3
    manager._tracking.best_favorable_move = 12.0
    manager._tracking.worst_adverse_move = -4.0

    payload = manager.snapshot_state()
    assert payload == {"bars_since_entry": 3, "mfe": 12.0, "mae": -4.0}

    fresh = ExitManager([])
    fresh.on_position_opened(_context())
    fresh.restore_state(payload)
    assert fresh.bars_since_entry == 3
    assert fresh.mfe == 12.0
    assert fresh._tracking.worst_adverse_move == -4.0


def test_exit_manager_restore_ignores_bad_payload() -> None:
    from strategy_policy.exit_rules import ExitManager

    manager = ExitManager([])
    manager.on_position_opened(_context())
    manager.restore_state({"bars_since_entry": "not-a-number"})
    assert manager.bars_since_entry == 0


# ── 5. strategy-level monotonic accumulation (recovery not cleared) ──

def test_strategy_hard_recovery_not_cleared_by_aligned() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
    from vnpy_chan.order_state import CtaOrderStatusMachine

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.pos = 0
    strategy.cta_engine = SimpleNamespace(
        main_engine=SimpleNamespace(
            get_engine=lambda name: SimpleNamespace(
                get_all_positions=lambda: [],
                get_all_active_orders=lambda: [],
            )
        )
    )
    strategy._order_state = CtaOrderStatusMachine()
    strategy._position_context = None
    strategy.write_log = lambda message: None
    strategy._ensure_runtime_state_helpers = ChanBspStrategy._ensure_runtime_state_helpers.__get__(strategy)
    strategy._ensure_runtime_state_helpers()

    # A hard recovery reason accumulates first (e.g. state restore failure)
    strategy._accumulate_recovery("runtime_state_invalid:state_version_mismatch")
    assert strategy._recovery_required

    # A later ALIGNED OMS result must NOT clear the hard reason
    strategy._check_oms_reconciliation()
    assert strategy._recovery_required is True
    assert "runtime_state_invalid" in strategy.recovery_reasons


def test_oms_absent_fails_closed() -> None:
    from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
    from vnpy_chan.order_state import CtaOrderStatusMachine

    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.vt_symbol = "rb2610.SHFE"
    strategy.pos = 0
    strategy.cta_engine = SimpleNamespace(main_engine=None)
    strategy._order_state = CtaOrderStatusMachine()
    strategy._position_context = None
    strategy.write_log = lambda message: None
    strategy._ensure_runtime_state_helpers = ChanBspStrategy._ensure_runtime_state_helpers.__get__(strategy)
    strategy._ensure_runtime_state_helpers()

    strategy._check_oms_reconciliation()

    assert strategy._recovery_required is True
    assert "oms_unavailable" in strategy.recovery_reasons
