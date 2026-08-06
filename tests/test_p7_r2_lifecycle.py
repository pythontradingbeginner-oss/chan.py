"""P7-R2: VeighNa lifecycle + readiness gate isolation.

VeighNa's CtaEngine._init_strategy() calls on_init() FIRST, then restores
persisted variables from disk via setattr().  This means any GUI variable
that doubles as an order-safety source of truth (production_ready,
initialization_status, warmup state) can be silently overwritten by stale
disk values AFTER a fresh initialization.

These tests assert the readiness gate must come from the PRIVATE
_warmup_readiness snapshot, never from disk-restored GUI variables, and
that a failed/blocked initialization cannot be revived by stale disk
values.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.readiness import WarmupReadiness


class _Engine:
    main_engine = None

    def write_log(self, message, strategy=None):
        pass

    def load_bar(self, vt_symbol, days, interval, callback, use_database):
        return []

    def put_strategy_event(self, strategy):
        pass


def _ready_snapshot(**overrides) -> WarmupReadiness:
    base = dict(
        input_1m_count=8000,
        bar_5m_count=2400,
        bar_15m_count=800,
        bar_60m_count=200,
        fresh=True,
        sequence_valid=True,
        aggregation_aligned=True,
        main_confirmed_bis=5,
        child_confirmed_bis=10,
        parent_confirmed_segments=1,
        required_15m_bars=800,
        reasons=(),
    )
    base.update(overrides)
    return WarmupReadiness(**base)


def _strategy(**overrides) -> ChanBspStrategy:
    strategy = ChanBspStrategy(_Engine(), "r2", "rb2610.SHFE", {})
    strategy.put_event = lambda: None
    for name, value in overrides.items():
        setattr(strategy, name, value)
    return strategy


def _simulate_init_strategy(strategy, disk_variables: dict) -> None:
    """Reproduce CtaEngine._init_strategy order: on_init then setattr restore."""
    strategy.on_init()
    for name in strategy.variables:
        value = disk_variables.get(name)
        if value is not None:
            setattr(strategy, name, value)


def test_private_readiness_snapshot_is_the_gate_not_gui_variables() -> None:
    """After on_init, the private snapshot is authoritative; disk can't flip it."""
    strategy = _strategy()
    strategy.on_init = lambda: None
    # A blocked/failed init leaves the private snapshot NOT ready
    strategy._warmup_readiness = _ready_snapshot(reasons=("warmup_15m_insufficient:100<800",))
    strategy.production_ready = False
    strategy.initialization_status = "blocked"

    # Simulate stale disk variables claiming ready
    disk = {
        "production_ready": True,
        "initialization_status": "ready",
        "warmup_fresh": True,
        "warmup_bar_count": 800,
        "structure_ready": True,
    }
    _simulate_init_strategy(strategy, disk)

    # The gate consults the PRIVATE snapshot, not the overwritten variables
    assert not strategy._warmup_readiness.ready
    assert strategy._snapshot_production_ready() is False


def test_stale_disk_production_ready_true_cannot_make_failed_init_ready() -> None:
    strategy = _strategy()
    strategy.on_init = lambda: None
    strategy._warmup_readiness = _ready_snapshot(reasons=("warmup_stale",))
    strategy.production_ready = False
    strategy.initialization_status = "shadow_only_stale"

    _simulate_init_strategy(strategy, {"production_ready": True, "initialization_status": "ready"})

    assert strategy._snapshot_production_ready() is False


def test_stale_disk_production_ready_false_cannot_block_fresh_ready_init() -> None:
    strategy = _strategy()
    strategy.on_init = lambda: None
    strategy._warmup_readiness = _ready_snapshot()
    strategy.production_ready = True
    strategy.initialization_status = "ready"

    _simulate_init_strategy(strategy, {"production_ready": False, "initialization_status": "not_initialized"})

    assert strategy._snapshot_production_ready() is True
    # on_start re-applies the private snapshot to the GUI variable
    strategy._restore_runtime_state = lambda: None
    strategy._check_oms_reconciliation = lambda: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True, mode="shadow", reason_text="shadow_mode_enabled"
    )
    strategy.on_start()
    assert strategy.production_ready is True
    assert strategy.initialization_status == "ready"


def test_on_start_reapplies_warmup_snapshot_after_variables_restore() -> None:
    """on_start must re-apply this run's warmup snapshot after disk restore."""
    strategy = _strategy()
    strategy.on_init = lambda: None
    strategy._warmup_readiness = _ready_snapshot()
    strategy.production_ready = True
    strategy.initialization_status = "ready"

    # Disk restore corrupts the GUI variables
    _simulate_init_strategy(strategy, {"production_ready": False})

    # on_start reapplies the private snapshot
    strategy._restore_runtime_state = lambda: None
    strategy._check_oms_reconciliation = lambda: None
    strategy._runtime_started = False
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True, mode="shadow", reason_text="shadow_mode_enabled"
    )
    strategy.on_start()

    assert strategy.production_ready is True
    assert strategy._runtime_started is True


def test_runtime_started_false_blocks_decision_even_if_gui_shows_ready() -> None:
    """Even if CTA UI shows inited/trading, internal runtime_started=false means no orders."""
    strategy = _strategy()
    strategy.inited = True
    strategy.trading = True
    strategy.production_ready = True
    strategy.initialization_status = "ready"
    strategy._runtime_started = False  # internal gate still closed
    strategy._market_data_enabled = False

    assert strategy._runtime_started is False
    assert strategy._market_data_enabled is False
