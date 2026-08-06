# -*- coding: utf-8 -*-
"""ChanBspStrategy: Chan Theory BSP Trend Strategy as vnpy CtaTemplate.

This is a thin adapter that wraps the entire chan.py signal pipeline
inside a standard CtaTemplate so it appears in vnpy's strategy dropdown.

Flow per N-minute bar:
  BarData -> CKLine_Unit -> CChan.trigger_load()
  -> GradedChanStrategy.evaluate_bar() -> SignalDecision -> signal
  -> ExitManager.check() (if position held)
  -> RiskManager.approve()
  -> vnpy buy/sell/short/cover

Place this file in: C:\\Users\\Administrator\\strategies\\
Ensure chan.py project is in PYTHONPATH.
"""

from __future__ import annotations

import sys
import json
from datetime import datetime
from pathlib import Path
import pandas as pd

from vnpy_ctastrategy import (
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)
from vnpy.trader.constant import Interval, Offset

# Ensure chan.py project is in sys.path
_CHAN_CANDIDATES = [
    Path("H:/Github/chan.py"),
    Path.home() / "Github" / "chan.py",
    Path(__file__).resolve().parent.parent,
]
for _cp in _CHAN_CANDIDATES:
    if _cp.exists() and str(_cp) not in sys.path:
        sys.path.insert(0, str(_cp))

from chan_futures.trade_intent import DecisionTraceRecord, PendingEntry, TradeIntent
from chan_futures.execution import adverse_fill_price
from data_foundation import (
    CalendarCoverageError,
    RBTradingCalendar,
    aggregate_continuous_1m_to_Nm,
)
from signal_core import SignalDirection, SignalState
from strategy_policy.position import PositionContext
from vnpy_chan.converter import bars_to_ohlc_frame
from vnpy_chan.hard_risk import HardRiskState, OpenDecision, evaluate_open_guard
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.oms_reconciliation import reconcile as reconcile_oms
from vnpy_chan.production_guard import (
    DEFAULT_RISK_MANAGER_SETTING_PATH,
    evaluate_production_gate,
)
from vnpy_chan.readiness import WarmupReadiness, WarmupValidationError
from vnpy_chan.runtime_state import (
    RuntimeStatePackage,
    RuntimeStateVersionError,
    STATE_VERSION,
)
from vnpy_chan.session_aggregator import (
    AggregationSequenceError,
    RbSessionBarAggregator,
    SUPPORTED_WINDOWS,
    normalize_realtime_bar,
)

# Config defaults
DEFAULT_CHAN_PROJECT = "H:/Github/chan.py"
DEFAULT_CONFIG_YAML = "configs/rb_15m_qingpai_strict.yaml"


class ChanBspStrategy(CtaTemplate):
    """Chan Theory BSP Trend Strategy (CtaTemplate adapter).

    Triggers on each N-minute bar:
      1. bar -> CKLine_Unit -> CChan.trigger_load()
      2. GradedChanStrategy.evaluate_bar() -> SignalDecision -> signal
      3. If holding a position: ExitManager.check() -> exit?
      4. If no position and signal triggers: buy/short
      5. If signal reverses: sell/cover
    """

    author = "chan.py"

    # --- vnpy parameters (adjustable in GUI) ---
    chan_project_path = DEFAULT_CHAN_PROJECT
    config_yaml = DEFAULT_CONFIG_YAML
    kl_window = 15
    fixed_size = 1
    load_days = 100
    production_ready = False
    operator_confirmed = False
    shadow_mode = True
    forward_confirmed = False
    risk_manager_confirmed = False
    risk_manager_setting_path = str(DEFAULT_RISK_MANAGER_SETTING_PATH)
    manual_halt = False
    minimum_equity = 0.0
    active_main_contracts = ""
    order_timeout_seconds = 30

    # --- vnpy variables (displayed in GUI) ---
    bi_count = 0
    seg_count = 0
    zs_count = 0
    last_bsp_type = ""
    last_bsp_direction = ""
    last_signal_grade = ""
    bars_processed = 0
    exit_reason = ""
    total_trades = 0
    total_pnl = 0.0
    peak_equity = 0.0
    production_status = "not_initialized"
    production_block_reason = ""
    risk_status = "unknown"
    order_status = "idle"
    recovery_status = "not_checked"
    recovery_reasons = ""
    hard_risk_status = "armed"
    hard_risk_reasons = ""
    last_tick_time_text = ""
    last_account_time_text = ""
    position_state_json = ""
    risk_state_json = ""
    order_state_json = ""
    runtime_state_json = ""
    initialization_status = "not_initialized"
    initialization_error = ""
    structure_ready = False
    warmup_fresh = False
    warmup_bar_count = 0
    calendar_valid_through = ""
    warmup_readiness_json = ""

    parameters = [
        "chan_project_path",
        "config_yaml",
        "kl_window",
        "fixed_size",
        "load_days",
        "operator_confirmed",
        "shadow_mode",
        "forward_confirmed",
        "risk_manager_confirmed",
        "risk_manager_setting_path",
        "manual_halt",
        "minimum_equity",
        "active_main_contracts",
        "order_timeout_seconds",
    ]
    variables = [
        "bi_count",
        "seg_count",
        "zs_count",
        "last_bsp_type",
        "last_bsp_direction",
        "last_signal_grade",
        "bars_processed",
        "exit_reason",
        "total_trades",
        "total_pnl",
        "peak_equity",
        "production_status",
        "production_block_reason",
        "risk_status",
        "order_status",
        "recovery_status",
        "recovery_reasons",
        "hard_risk_status",
        "hard_risk_reasons",
        "last_tick_time_text",
        "last_account_time_text",
        "position_state_json",
        "risk_state_json",
        "order_state_json",
        "runtime_state_json",
        "production_ready",
        "initialization_status",
        "initialization_error",
        "structure_ready",
        "warmup_fresh",
        "warmup_bar_count",
        "calendar_valid_through",
        "warmup_readiness_json",
    ]

    # --- Constructor ---

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # Ensure project path is importable
        for p in [self.chan_project_path, DEFAULT_CHAN_PROJECT]:
            if p and Path(p).exists() and str(p) not in sys.path:
                sys.path.insert(0, str(p))

        # BarGenerator is used only for tick -> 1m.  N-minute aggregation is
        # session-aware and mirrors data_foundation historical replay rules.
        self._calendar = RBTradingCalendar.load_default()
        self.bg = BarGenerator(self._on_realtime_minute_bar)
        self._kl_aggregator = self._make_session_aggregator(self.kl_window)
        self._parent_aggregator = None
        self._child_aggregator = None
        self._parent_kl_type = None
        self._child_kl_type = None

        # Lazy-initialized chan components
        self._chan = None
        self._wrapper = None
        self._extractor = None
        self._decision_kernel = None
        self._exit_manager = None
        self._risk = None
        self._config = None
        self._production_gate = None
        self._warmup_readiness = WarmupReadiness()
        self._order_state = CtaOrderStatusMachine()
        self._runtime_state_restored = False
        self._recovery_required = False
        self._recovery_result = None
        self._last_tick_time = None
        self._last_account_time = None

        self._position_context: PositionContext | None = None
        self._pending_entry: PendingEntry | None = None
        self._pending_reversal: PendingEntry | None = None
        self._trade_pnl_batch: list[float] = []
        self._previous_close: float | None = None
        self._true_ranges: list[float] = []
        self._warmup_in_progress = False
        self._market_data_enabled = False
        self._runtime_started = False
        self._recovery_observed = False
        self._level_counts = {5: 0, 15: 0, 60: 0}
        self._level_timestamps: dict[int, list[pd.Timestamp]] = {
            5: [],
            15: [],
            60: [],
        }
        self._fresh_through: pd.Timestamp | None = None

    # ============================================================
    # vnpy Lifecycle
    # ============================================================

    def on_init(self):
        """Initialize: load historical bars and warm up CChan."""
        self.write_log(f"ChanBspStrategy init: {self.vt_symbol}")
        self._reset_initialization_state()
        try:
            self._init_chan_pipeline()
            now = self._initialization_now()
            bars = self._load_warmup_bars()
            prepared, fresh_through, fresh = self._prepare_warmup_bars(bars, now)
            self._fresh_through = fresh_through
            if not fresh:
                latest = _bar_timestamp(prepared[-1]) if prepared else None
                detail = f"latest={latest};required={fresh_through}"
                snapshot = WarmupReadiness(
                    input_1m_count=len(prepared),
                    last_1m=latest.to_pydatetime() if latest is not None else None,
                    fresh=False,
                    fresh_through=fresh_through.to_pydatetime(),
                    reasons=("warmup_stale",),
                )
                self._apply_warmup_readiness(snapshot)
                self._block_initialization("shadow_only_stale", "warmup_stale", detail)
                return

            self._replay_warmup(prepared)
            snapshot = self._build_warmup_readiness(prepared, fresh_through)
            self._apply_warmup_readiness(snapshot)
            if not snapshot.ready:
                self._block_initialization(
                    "blocked",
                    snapshot.reasons[0],
                    ",".join(snapshot.reasons),
                )
                return

            self.initialization_status = "ready"
            self.initialization_error = ""
            self.production_ready = True
            self._market_data_enabled = True
            gate = self._refresh_production_gate()
            self.write_log(
                "Initialization ready: "
                f"1m={snapshot.input_1m_count} 5m={snapshot.bar_5m_count} "
                f"15m={snapshot.bar_15m_count} 60m={snapshot.bar_60m_count} "
                f"mode={gate.mode}"
            )
        except (CalendarCoverageError, WarmupValidationError, AggregationSequenceError) as exc:
            reason = getattr(exc, "reason", "calendar_out_of_range")
            detail = getattr(exc, "detail", "") or str(exc)
            if str(detail) == str(reason):
                detail = ""
            self._block_initialization("blocked", str(reason), str(detail))
        except Exception as exc:
            self.initialization_status = "failed"
            self.initialization_error = f"{type(exc).__name__}: {exc}"
            self.production_ready = False
            self._market_data_enabled = False
            self.write_log(f"Initialization failed: {self.initialization_error}")
            raise
        finally:
            self.put_event()

    def on_start(self):
        """Start trading."""
        # VeighNa restores persisted variables AFTER on_init (CtaEngine
        # _init_strategy: call on_init, then setattr each variable from
        # strategy_data).  Re-apply this run's private warmup snapshot so
        # stale disk values cannot flip the readiness gate.
        self._reapply_warmup_readiness()
        self._restore_runtime_state()
        self._check_oms_reconciliation()
        try:
            self._register_live_events()
            gate = self._refresh_production_gate()
        except Exception:
            self._unregister_live_events()
            raise
        self._runtime_started = bool(
            gate.ready and self.production_ready and not self._recovery_required
        )
        self.write_log(
            f"ChanBspStrategy started mode={gate.mode} "
            f"recovery={self.recovery_status} "
            f"reason={gate.reason_text or 'ready'}"
        )

    def _register_live_events(self) -> None:
        """Register EVENT_TIMER and EVENT_ACCOUNT for realtime safety.

        CtaEngine does NOT register EVENT_TIMER, so CtaTemplate.on_timer
        is never called.  We register our own bridge via the engine's
        EventEngine to drive order timeouts even when the market is
        disconnected.

        Registration is idempotent (second call cancels the first).  The
        EventEngine callbacks funnel through call_strategy_func to retain
        VeighNa's exception safe-stop semantics.
        """
        try:
            event_engine = getattr(self.cta_engine, "event_engine", None)
        except Exception:
            event_engine = None
        if event_engine is None:
            self.write_log("No EventEngine available; timer/account events not registered")
            return

        handler = getattr(self, "_live_timer_handler", None)
        if handler is not None:
            self._unregister_live_events()

        try:
            from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TIMER
        except ImportError:
            return

        def _timer_handler(event):
            try:
                self.cta_engine.call_strategy_func(self, self.on_timer)
            except Exception:
                pass

        def _account_handler(event):
            try:
                data = getattr(event, "data", None)
                if data is None:
                    return
                # P7-R11: only update heartbeat when the account event's
                # gateway matches this strategy's gateway.  Otherwise a
                # different gateway's account update would spuriously keep
                # the heartbeat alive.
                strategy_gateway = getattr(self, "_resolved_gateway", "")
                if not strategy_gateway:
                    strategy_gateway = self._resolve_gateway(
                        getattr(self.cta_engine, "main_engine", None),
                        str(getattr(self, "vt_symbol", "")),
                    )
                    if not strategy_gateway:
                        return
                    self._resolved_gateway = strategy_gateway
                evt_gw = str(getattr(data, "gateway_name", "") or "")
                if not evt_gw or evt_gw.upper() != strategy_gateway.upper():
                    return
                self._last_account_time = datetime.now()
                self.last_account_time_text = (
                    self._last_account_time.isoformat(timespec="seconds")
                )
            except Exception:
                pass

        self._live_timer_handler = _timer_handler
        self._live_account_handler = _account_handler
        event_engine.register(EVENT_TIMER, _timer_handler)
        event_engine.register(EVENT_ACCOUNT, _account_handler)

    def _unregister_live_events(self) -> None:
        handler = getattr(self, "_live_timer_handler", None)
        acc_handler = getattr(self, "_live_account_handler", None)
        try:
            event_engine = getattr(self.cta_engine, "event_engine", None)
        except Exception:
            event_engine = None
        if event_engine is not None:
            try:
                from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TIMER
            except ImportError:
                return
            if handler is not None:
                event_engine.unregister(EVENT_TIMER, handler)
            if acc_handler is not None:
                event_engine.unregister(EVENT_ACCOUNT, acc_handler)
        self._live_timer_handler = None
        self._live_account_handler = None

    def on_stop(self):
        """Stop trading."""
        self._runtime_started = False
        self._unregister_live_events()
        self.write_log(
            f"ChanBspStrategy stopped: "
            f"trades={self.total_trades} pnl={self.total_pnl:.0f}pts"
        )

    # ============================================================
    # Bar handling
    # ============================================================

    def on_tick(self, tick: TickData):
        """Tick -> BarGenerator."""
        self._last_tick_time = datetime.now()
        self.last_tick_time_text = self._last_tick_time.isoformat(timespec="seconds")
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """Consume an already end-labelled historical/database minute."""
        if not self._warmup_in_progress and not (
            self._market_data_enabled and self._runtime_started
        ):
            return
        try:
            self._process_end_labeled_bar(
                bar,
                allow_execution=not self._warmup_in_progress,
            )
        except (AggregationSequenceError, CalendarCoverageError) as exc:
            if self._warmup_in_progress:
                raise
            self._block_market_data(exc)

    def _on_realtime_minute_bar(self, bar: BarData) -> None:
        """Normalize VeighNa minute-start labels before shared aggregation."""
        normalized = normalize_realtime_bar(bar)
        if not (self._market_data_enabled and self._runtime_started):
            if self.initialization_status == "shadow_only_stale":
                self._observe_recovery_bar(normalized)
            return
        try:
            self._process_end_labeled_bar(normalized, allow_execution=True)
        except (AggregationSequenceError, CalendarCoverageError) as exc:
            self._block_market_data(exc)

    def _process_end_labeled_bar(
        self,
        bar: BarData,
        *,
        allow_execution: bool,
    ) -> None:
        """1m end-labelled bar -> session-aware 5m/15m/60m bars."""
        # Complete child and parent bars first.  Evidence sharing the same
        # close timestamp is therefore visible to the current-level decision.
        if self._child_aggregator is not None:
            child_bar = self._child_aggregator.update_bar(bar)
            if child_bar is not None:
                self._on_child_bar(child_bar)
        if self._parent_aggregator is not None:
            parent_bar = self._parent_aggregator.update_bar(bar)
            if parent_bar is not None:
                self._on_parent_bar(parent_bar)
        if self._kl_aggregator is None:
            self._on_kl_bar(bar, allow_execution=allow_execution)
            return
        kl_bar = self._kl_aggregator.update_bar(bar)
        if kl_bar is not None:
            self._on_kl_bar(kl_bar, allow_execution=allow_execution)

    def _on_parent_bar(self, bar: BarData):
        if self._decision_kernel is None or self._parent_kl_type is None:
            return
        available_at = self._window_end_time(bar, self._parent_kl_type)
        klu = self._bar_to_klu(
            bar,
            kl_type=self._parent_kl_type,
            timestamp=available_at,
        )
        self._decision_kernel.observe_parent_bar(klu, available_at=available_at)
        self._record_level_bar(60, bar)

    def _on_child_bar(self, bar: BarData):
        if self._decision_kernel is None or self._child_kl_type is None:
            return
        available_at = self._window_end_time(bar, self._child_kl_type)
        klu = self._bar_to_klu(
            bar,
            kl_type=self._child_kl_type,
            timestamp=available_at,
        )
        self._decision_kernel.observe_child_bar(klu, available_at=available_at)
        self._record_level_bar(5, bar)

    def _on_kl_bar(self, bar: BarData, *, allow_execution: bool = True):
        """N-minute bar completed. Main strategy logic."""
        self.bars_processed += 1

        if self._chan is None or self._decision_kernel is None:
            return

        # 1. Feed bar to CChan
        timestamp = self._window_end_time(bar, self._current_kl_type())
        klu = self._bar_to_klu(bar, timestamp=timestamp)
        self._chan.trigger_load({klu.kl_type: [klu]})
        self._record_level_bar(int(self.kl_window), bar)

        price = float(bar.close_price)
        atr = self._update_atr(bar)
        self._decision_kernel.observe_structure(
            chan=self._chan,
            timestamp=timestamp,
            lv_idx=0,
        )

        # 2. Update GUI display
        self._update_gui_vars()

        # 3. Build the same current-bar intent before applying exit priority.
        intent = self._decision_kernel.evaluate_bar(
            chan=self._chan,
            current_position=int(self.pos) if allow_execution else 0,
            price=price,
            timestamp=timestamp,
            active_symbol=bar.symbol,
            lv_idx=0,
            account_equity=self._account_equity(),
            available_funds=self._account_available_funds(),
            atr=atr,
        )

        # 4. Check exit rules, including confirmed opposite BSP and chan momentum.
        if allow_execution and self.pos != 0 and self._exit_manager is not None:
            exit_sig = self._check_exit(bar, price, timestamp, intent=intent, atr=atr)
            if exit_sig is not None:
                self._pending_reversal = None
                self.exit_reason = exit_sig.reason_code
                self._close_existing(
                    bar, exit_sig.reason_code, price=exit_sig.exit_price
                )
                self.put_event()
                return

        # 5. Consume the already-audited TradeIntent.
        if intent is not None:
            self.last_signal_grade = intent.grade
            if not allow_execution:
                return
            if not intent.accepted:
                reasons = ",".join(intent.decision.reason_codes)
                self.write_log(f"Decision rejected: {reasons}")
                self.put_event()
                return

            signal = intent.signal
            if self._risk is not None:
                risk_decision = self._risk.approve(
                    signal,
                    current_equity=self._account_equity(),
                )
                if not risk_decision.approved:
                    self.write_log(f"Risk rejected: {risk_decision.reason}")
                    self.risk_status = risk_decision.reason
                    self.put_event()
                    return
                self.risk_status = "approved"

            if self.pos == 0:
                self._submit_open_intent(intent)
            elif signal.target_position == 0:
                self._pending_reversal = None
                self.exit_reason = "strategy_close"
                self._close_existing(bar, "strategy_close")
            elif (self.pos > 0) != (signal.target_position > 0):
                self._pending_reversal = PendingEntry(intent)
                self.exit_reason = "strategy_reverse"
                self._close_existing(bar, "strategy_reverse")

        if allow_execution:
            self.put_event()

    # ============================================================
    # Order/Trade callbacks
    # ============================================================

    def on_trade(self, trade: TradeData):
        """Track PnL on close.

        Only an ACCEPTED disposition updates PositionContext / risk / PnL.
        duplicate/late/unknown/invalid_transition fills never touch business
        state, so a restart replay or out-of-order report cannot double-count.
        """
        self._ensure_runtime_state_helpers()
        result = self._order_state.on_trade(trade)
        self.order_status = self._order_state.summary
        if not result.accepted:
            self.write_log(
                f"Trade ignored disposition={result.disposition.value} "
                f"vt_tradeid={getattr(trade, 'vt_tradeid', '')}"
            )
            self.put_event()
            return
        if trade.offset == Offset.OPEN:
            fill_volume = int(trade.volume)
            pending = self._pending_entry
            if pending is None:
                self.write_log("OPEN FILL IGNORED: missing TradeIntent")
                self.put_event()
                return

            opening_position = self._position_context is None
            if opening_position:
                self._position_context = pending.intent.position_from_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None) or datetime.now(),
                    entry_bar=self.bars_processed,
                    active_symbol=getattr(trade, "symbol", None),
                )
            else:
                self._position_context = self._position_context.merge_open_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None),
                )
            self._pending_entry = pending.apply_fill(fill_volume)
            if self._pending_entry.complete:
                self._pending_entry = None
            if self._exit_manager is not None:
                if opening_position:
                    self._exit_manager.on_position_opened(self._position_context)
                else:
                    self._exit_manager.on_position_updated(self._position_context)
            self.write_log(
                f"OPEN FILLED: @ {trade.price:.1f} volume={fill_volume}"
            )
            self._persist_runtime_state()
            self.put_event()
            return

        if self._position_context is not None and trade.offset == Offset.CLOSE:
            fill_volume = int(trade.volume)
            fee = self._config.execution.fee_points if self._config else 1.0
            pnl = self._position_context.pnl_points(
                exit_price=float(trade.price),
                fee_points=fee,
                volume=fill_volume,
            )
            self.total_pnl += pnl
            self.total_trades += 1
            self._trade_pnl_batch.append(pnl)
            self._position_context = self._position_context.reduce_volume(fill_volume)

            if self._risk is not None:
                self._risk.on_fill(
                    pnl_points=pnl,
                    fill_time=getattr(trade, "datetime", None) or datetime.now(),
                    current_equity=self._account_equity(),
                )

            self.write_log(
                f"CLOSE: @ {trade.price:.1f} PnL={pnl:.0f}pts "
                f"cumulative={self.total_pnl:.0f}pts trades={self.total_trades}"
            )
            if self._position_context is None:
                self._pending_entry = None
                self.exit_reason = ""
                if self._exit_manager is not None:
                    self._exit_manager.on_close()
                pending_reversal = self._pending_reversal
                self._pending_reversal = None
                if pending_reversal is not None:
                    self._submit_open_intent(pending_reversal.intent)
            elif self._exit_manager is not None:
                self._exit_manager.on_position_updated(self._position_context)

        self._persist_runtime_state()
        self.put_event()

    def on_order(self, order: OrderData):
        self._ensure_runtime_state_helpers()
        tracked = self._order_state.on_order(order)
        self.order_status = self._order_state.summary
        vt_orderid = str(getattr(order, "vt_orderid", ""))
        if not tracked.active:
            if tracked.role.startswith("open") and tracked.traded <= 0:
                self._pending_entry = None
            if (
                self._order_state.close_intent_orderid == vt_orderid
            ):
                self._order_state.clear_close_intent()
        self._persist_runtime_state()
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        self._ensure_runtime_state_helpers()
        self._order_state.on_stop_order(stop_order)
        self.order_status = self._order_state.summary
        self._persist_runtime_state()
        self.put_event()

    def on_timer(self):
        """Reliable timer path: drive order timeouts even when the market is
        disconnected.  Timeout first requests a REAL cancel (CANCELLING) and
        only archives after the exchange reports a terminal status."""
        self._ensure_runtime_state_helpers()
        if not self._runtime_started:
            return
        try:
            cancelling = self._order_state.check_timeouts()
        except Exception as exc:
            self.write_log(f"Order timeout check failed: {exc}")
            return
        for vt_orderid in cancelling:
            self.cancel_order(vt_orderid)
            self.write_log(f"Order timed out, cancelling: {vt_orderid}")

    # ============================================================
    # Internal: deterministic warmup and readiness
    # ============================================================

    def _reset_initialization_state(self) -> None:
        self.production_ready = False
        self.initialization_status = "initializing"
        self.initialization_error = ""
        self.structure_ready = False
        self.warmup_fresh = False
        self.warmup_bar_count = 0
        self.calendar_valid_through = str(self._calendar.valid_through.date())
        self.warmup_readiness_json = ""
        self._market_data_enabled = False
        self._runtime_started = False
        self._recovery_observed = False
        self._warmup_readiness = WarmupReadiness()
        self._fresh_through = None
        self._level_counts = {5: 0, 15: 0, 60: 0}
        self._level_timestamps = {5: [], 15: [], 60: []}
        self.bars_processed = 0
        self._previous_close = None
        self._true_ranges = []
        for aggregator in self._session_aggregators():
            aggregator.reset()

    @staticmethod
    def _initialization_now() -> datetime:
        return pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).to_pydatetime()

    def _load_warmup_bars(self) -> list[BarData]:
        if int(self.load_days) <= 0:
            raise WarmupValidationError("warmup_empty", "load_days must be positive")
        self.write_log(
            f"Loading {self.load_days} days from VeighNa SQLite (use_database=True)..."
        )
        bars = self.cta_engine.load_bar(
            self.vt_symbol,
            int(self.load_days),
            Interval.MINUTE,
            self.on_bar,
            True,
        )
        return list(bars or [])

    def _prepare_warmup_bars(
        self,
        bars: list[BarData],
        now: datetime,
    ) -> tuple[list[BarData], pd.Timestamp, bool]:
        if not bars:
            raise WarmupValidationError("warmup_empty", "SQLite returned no minute bars")

        timestamps = [_bar_timestamp(bar) for bar in bars]
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            reason = (
                "warmup_duplicate_minute"
                if len(set(timestamps)) != len(timestamps)
                else "warmup_out_of_order"
            )
            raise WarmupValidationError(reason)

        expected_symbol = self.vt_symbol.rsplit(".", 1)[0].lower()
        symbols = {str(getattr(bar, "symbol", "")).lower() for bar in bars}
        if symbols != {expected_symbol}:
            raise WarmupValidationError(
                "warmup_contract_mismatch",
                f"expected={expected_symbol};actual={sorted(symbols)}",
            )

        now_stamp = _timestamp_naive(pd.Timestamp(now))
        next_day = self._calendar.next_trading_day(now_stamp)
        self._calendar.validate_coverage(
            min(timestamps),
            max(next_day, now_stamp.normalize()),
        )
        fresh_through = self._calendar.latest_complete_session_end(now_stamp)
        timestamp_set = set(timestamps)
        fresh = max(timestamps) >= fresh_through and fresh_through in timestamp_set
        if not fresh:
            return bars, fresh_through, False

        mapped = self._calendar.map_datetimes(pd.Series(timestamps))
        if mapped["trading_day"].isna().any():
            bad_index = int(mapped["trading_day"].isna().to_numpy().nonzero()[0][0])
            raise WarmupValidationError(
                "warmup_out_of_session",
                str(timestamps[bad_index]),
            )

        replay_bars = [
            bar for bar, timestamp in zip(bars, timestamps) if timestamp <= fresh_through
        ]
        replay_times = [_bar_timestamp(bar) for bar in replay_bars]
        replay_mapped = self._calendar.map_datetimes(pd.Series(replay_times))
        start_candidates = [
            index
            for index, value in enumerate(replay_mapped["session_minute_index"])
            if int(value) == 1
        ]
        if not start_candidates:
            raise WarmupValidationError(
                "warmup_no_complete_session",
                "query does not contain a session boundary",
            )
        replay_bars = replay_bars[start_candidates[0] :]
        if not replay_bars or _bar_timestamp(replay_bars[-1]) != fresh_through:
            raise WarmupValidationError(
                "warmup_incomplete_latest_session",
                f"required={fresh_through}",
            )
        return replay_bars, fresh_through, True

    def _replay_warmup(self, bars: list[BarData]) -> None:
        self._warmup_in_progress = True
        try:
            for bar in bars:
                self._process_end_labeled_bar(bar, allow_execution=False)
            for frequency, aggregator in self._aggregators_by_frequency().items():
                completed = aggregator.flush()
                if completed is None:
                    continue
                if frequency == int(self.kl_window):
                    self._on_kl_bar(completed, allow_execution=False)
                elif frequency == 5:
                    self._on_child_bar(completed)
                elif frequency == 60:
                    self._on_parent_bar(completed)
        finally:
            self._warmup_in_progress = False

    def _build_warmup_readiness(
        self,
        bars: list[BarData],
        fresh_through: pd.Timestamp,
    ) -> WarmupReadiness:
        frame = bars_to_ohlc_frame(bars)
        expected: dict[int, pd.DataFrame] = {
            frequency: aggregate_continuous_1m_to_Nm(
                frame,
                frequency,
                calendar=self._calendar,
            )
            for frequency in (5, 15, 60)
        }
        aligned = all(
            self._level_timestamps[frequency]
            == [
                _timestamp_naive(pd.Timestamp(value))
                for value in expected[frequency]["datetime"]
            ]
            for frequency in (5, 15, 60)
        )
        rejected = {
            frequency: sum(
                audit.status == "DROPPED" and audit.reason != "SESSION_TAIL"
                for audit in aggregator.audits
            )
            for frequency, aggregator in self._aggregators_by_frequency().items()
        }
        main_confirmed_bis = sum(
            bool(bi.is_sure) for bi in self._chan[0].bi_list
        )
        multi = self._decision_kernel.multi_level_readiness
        child_confirmed_bis = multi.child_confirmed_bis if multi is not None else 0
        parent_confirmed_segments = (
            multi.parent_confirmed_segments if multi is not None else 0
        )
        required = int(getattr(self._config.production, "warmup_bars", 800))
        reasons: list[str] = []
        if self._level_counts[15] < required:
            reasons.append(
                f"warmup_15m_insufficient:{self._level_counts[15]}<{required}"
            )
        if not aligned:
            reasons.append("warmup_aggregation_mismatch")
        if any(rejected.get(frequency, 0) for frequency in (5, 15, 60)):
            reasons.append("warmup_aggregation_rejected")
        if main_confirmed_bis <= 0:
            reasons.append("main_confirmed_bi_missing")
        if child_confirmed_bis <= 0:
            reasons.append("child_confirmed_bi_missing")
        if parent_confirmed_segments <= 0:
            reasons.append("parent_confirmed_segment_missing")

        return WarmupReadiness(
            input_1m_count=len(bars),
            bar_5m_count=self._level_counts[5],
            bar_15m_count=self._level_counts[15],
            bar_60m_count=self._level_counts[60],
            last_1m=_bar_timestamp(bars[-1]).to_pydatetime(),
            last_5m=_last_datetime(self._level_timestamps[5]),
            last_15m=_last_datetime(self._level_timestamps[15]),
            last_60m=_last_datetime(self._level_timestamps[60]),
            rejected_5m_count=rejected.get(5, 0),
            rejected_15m_count=rejected.get(15, 0),
            rejected_60m_count=rejected.get(60, 0),
            fresh=True,
            sequence_valid=not any(rejected.values()),
            aggregation_aligned=aligned,
            main_confirmed_bis=main_confirmed_bis,
            child_confirmed_bis=child_confirmed_bis,
            parent_confirmed_segments=parent_confirmed_segments,
            required_15m_bars=required,
            fresh_through=fresh_through.to_pydatetime(),
            reasons=tuple(reasons),
        )

    def _apply_warmup_readiness(self, snapshot: WarmupReadiness) -> None:
        self._warmup_readiness = snapshot
        self.warmup_fresh = snapshot.fresh
        self.warmup_bar_count = snapshot.bar_15m_count
        self.structure_ready = snapshot.structure_ready
        self.warmup_readiness_json = json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _reapply_warmup_readiness(self) -> None:
        """Re-derive order-safety flags from the private snapshot.

        VeighNa restores persisted variables (including production_ready,
        initialization_status, warmup_*) over the strategy AFTER on_init.
        Those GUI variables are display-only; the private _warmup_readiness
        snapshot is the single source of truth for the order gate.  This must
        run in on_start, after the engine's variable restore.
        """
        snapshot = self._warmup_readiness
        if snapshot.ready:
            if self.initialization_status not in {
                "shadow_only_stale",
                "market_data_blocked",
                "failed",
            }:
                self.initialization_status = "ready"
                self.production_ready = True
        else:
            self.production_ready = False
            if self.initialization_status == "ready":
                self.initialization_status = "blocked"
                self.initialization_error = (
                    self.initialization_error
                    or ";".join(snapshot.reasons)
                )
        self.warmup_fresh = snapshot.fresh
        self.warmup_bar_count = snapshot.bar_15m_count
        self.structure_ready = snapshot.structure_ready

    def _block_initialization(
        self,
        status: str,
        reason: str,
        detail: str = "",
    ) -> None:
        self.initialization_status = status
        self.initialization_error = reason if not detail else f"{reason}: {detail}"
        self.production_ready = False
        self._market_data_enabled = False
        self._runtime_started = False
        self._refresh_production_gate()
        self.write_log(
            f"Initialization blocked status={status} reason={self.initialization_error}"
        )

    def _block_market_data(self, exc: Exception) -> None:
        self.initialization_status = "market_data_blocked"
        self.initialization_error = f"{type(exc).__name__}: {exc}"
        self.production_ready = False
        self._market_data_enabled = False
        self._runtime_started = False
        self._refresh_production_gate()
        self.write_log(f"Market data blocked: {self.initialization_error}")
        self.put_event()

    def _observe_recovery_bar(self, bar: BarData) -> None:
        if self._recovery_observed or self._fresh_through is None:
            return
        if _bar_timestamp(bar) >= self._fresh_through:
            self._recovery_observed = True
            self.write_log(
                "Fresh market data observed while shadow_only_stale; "
                "manual reinitialization is required"
            )

    def _record_level_bar(self, frequency: int, bar: BarData) -> None:
        if frequency not in self._level_counts:
            return
        self._level_counts[frequency] += 1
        self._level_timestamps[frequency].append(_bar_timestamp(bar))

    def _session_aggregators(self) -> list[RbSessionBarAggregator]:
        values = [
            self._child_aggregator,
            self._kl_aggregator,
            self._parent_aggregator,
        ]
        return [value for value in values if value is not None]

    def _aggregators_by_frequency(self) -> dict[int, RbSessionBarAggregator]:
        return {
            aggregator.frequency_minutes: aggregator
            for aggregator in self._session_aggregators()
        }

    # ============================================================
    # Internal: initialize chan pipeline (lazy)
    # ============================================================

    def _init_chan_pipeline(self):
        """Import and construct all chan.py components."""
        try:
            from Chan import CChan
            from ChanConfig import CChanConfig
            from Common.CEnum import AUTYPE, KL_TYPE

            from chan_futures.config_loader import (
                load_config,
                make_exit_manager,
                make_runtime_decision_kernel,
            )
            from chan_futures.risk import RiskConfig, RiskManager
        except ImportError as e:
            self.write_log(f"Import error: {e}. Check chan_project_path.")
            return

        # Load config
        config_path = Path(self.chan_project_path) / self.config_yaml
        if not config_path.is_file():
            raise FileNotFoundError(
                f"P7 strategy config not found: {config_path.resolve()}"
            )
        self._config = load_config(str(config_path))

        cfg = self._config

        # KL_TYPE mapping
        w2k = {
            1: KL_TYPE.K_1M, 5: KL_TYPE.K_5M, 15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M, 60: KL_TYPE.K_60M,
        }
        kl_type = w2k.get(self.kl_window, KL_TYPE.K_15M)

        # CChan
        self._chan = CChan(
            code=self.vt_symbol,
            begin_time=None,
            end_time=None,
            data_src="csv",
            lv_list=[kl_type],
            config=CChanConfig(cfg.chan.to_dict()),
            autype=AUTYPE.NONE,
        )

        # Shared decision kernel
        tf_str = kl_type.name.replace("K_", "").replace("M", "m")
        self._decision_kernel = make_runtime_decision_kernel(
            cfg,
            symbol="RB",
            timeframe=tf_str,
        )
        self._wrapper = self._decision_kernel.strategy
        self._extractor = self._decision_kernel.extractor

        if cfg.multi_level.enabled:
            level_to_window = {
                "K_1M": 1,
                "K_5M": 5,
                "K_15M": 15,
                "K_30M": 30,
                "K_60M": 60,
            }
            self._parent_kl_type = w2k[level_to_window[cfg.multi_level.parent_kl_type]]
            self._child_kl_type = w2k[level_to_window[cfg.multi_level.child_kl_type]]
            self._parent_aggregator = self._make_session_aggregator(
                level_to_window[cfg.multi_level.parent_kl_type],
            )
            self._child_aggregator = self._make_session_aggregator(
                level_to_window[cfg.multi_level.child_kl_type],
            )

        # ExitManager
        self._exit_manager = make_exit_manager(cfg)

        # RiskManager
        self._risk = RiskManager(
            RiskConfig(
                max_abs_position=cfg.risk.max_abs_position,
                max_loss_points=cfg.risk.max_loss_points,
                daily_loss_limit=cfg.risk.daily_loss_limit,
                max_consecutive_losses=cfg.risk.max_consecutive_losses,
                max_drawdown_pct=cfg.risk.max_drawdown_pct,
            )
        )

    # ============================================================
    # Internal: bar conversion
    # ============================================================

    def _bar_to_klu(
        self,
        bar: BarData,
        *,
        kl_type=None,
        timestamp: object | None = None,
    ):
        """Convert vnpy BarData to chan.py CKLine_Unit."""
        from Common.CEnum import DATA_FIELD, KL_TYPE
        from Common.CTime import CTime
        from KLine.KLine_Unit import CKLine_Unit

        ts = pd.Timestamp(timestamp if timestamp is not None else bar.datetime)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)

        w2k = {
            1: KL_TYPE.K_1M, 5: KL_TYPE.K_5M, 15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M, 60: KL_TYPE.K_60M,
        }
        kl_type = kl_type or w2k.get(self.kl_window, KL_TYPE.K_15M)

        item = {
            DATA_FIELD.FIELD_TIME: CTime(
                ts.year, ts.month, ts.day,
                ts.hour, ts.minute, ts.second,
                auto=False,
            ),
            DATA_FIELD.FIELD_OPEN: float(bar.open_price),
            DATA_FIELD.FIELD_HIGH: float(bar.high_price),
            DATA_FIELD.FIELD_LOW: float(bar.low_price),
            DATA_FIELD.FIELD_CLOSE: float(bar.close_price),
            DATA_FIELD.FIELD_VOLUME: float(getattr(bar, "volume", 0) or 0),
        }
        klu = CKLine_Unit(item, autofix=True)
        klu.kl_type = kl_type
        return klu

    def _current_kl_type(self):
        from Common.CEnum import KL_TYPE

        return {
            1: KL_TYPE.K_1M,
            5: KL_TYPE.K_5M,
            15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M,
            60: KL_TYPE.K_60M,
        }.get(self.kl_window, KL_TYPE.K_15M)

    @staticmethod
    def _window_end_time(bar: BarData, kl_type) -> datetime:
        return pd.Timestamp(bar.datetime).to_pydatetime()

    def _make_session_aggregator(self, window: int):
        if int(window) == 1:
            return None
        if int(window) not in SUPPORTED_WINDOWS:
            supported = ", ".join(str(value) for value in sorted(SUPPORTED_WINDOWS))
            raise ValueError(
                f"ChanBspStrategy supports session aggregation windows: {supported}"
            )
        return RbSessionBarAggregator(int(window), calendar=self._calendar)

    # ============================================================
    # Exit checking
    # ============================================================

    def _check_exit(
        self,
        bar: BarData,
        price: float,
        timestamp,
        *,
        intent: TradeIntent | None,
        atr: float | None,
    ):
        """Run ExitManager.check() for current position."""
        if self._exit_manager is None or not self._exit_manager.is_active:
            return None

        direction = "long" if self.pos > 0 else "short"
        snapshot = self._decision_kernel.build_exit_snapshot(
            self._chan,
            observed_at=timestamp,
            direction=direction,
            open_price=float(bar.open_price),
            high_price=float(bar.high_price),
            low_price=float(bar.low_price),
            close_price=price,
            average_amplitude=atr,
            lv_idx=0,
        )
        return self._exit_manager.check(
            bar_end_time=timestamp,
            open=float(bar.open_price),
            high=float(bar.high_price),
            low=float(bar.low_price),
            close=price,
            opposite_signal_triggered=_confirmed_opposite(intent, int(self.pos)),
            chan_snapshot=snapshot,
        )

    def _close_existing(
        self, bar: BarData, reason: str, *, price: float | None = None
    ):
        """Close current position.

        Enforces a single active close intent: consecutive bars must not pile
        up duplicate close orders before the first reaches a terminal report.
        """
        if self._order_state.has_active_close_intent():
            self.write_log(
                "Close skipped: single_active_close_intent "
                f"(existing {self._order_state.close_intent_orderid})"
            )
            return
        order_price = float(bar.close_price) if price is None else float(price)
        order_price = self._execution_price(order_price, -int(self.pos))
        if self.pos > 0:
            order_ids = self._send_live_or_shadow(
                "close",
                lambda: self.sell(order_price, abs(self.pos)),
                price=order_price,
                volume=abs(self.pos),
                closing=True,
                reason=reason,
            )
        elif self.pos < 0:
            order_ids = self._send_live_or_shadow(
                "close",
                lambda: self.cover(order_price, abs(self.pos)),
                price=order_price,
                volume=abs(self.pos),
                closing=True,
                reason=reason,
            )
        else:
            order_ids = []
        if order_ids:
            self._order_state.submit(
                order_ids,
                role="close",
                price=order_price,
                volume=abs(self.pos),
                reason=reason,
            )
            if order_ids:
                self._order_state.set_close_intent(order_ids[0])
            self.order_status = "pending_close"
            self._persist_runtime_state()

    def _submit_open_intent(self, intent: TradeIntent) -> None:
        signal = intent.signal
        planned_size = abs(signal.target_position)
        if planned_size <= 0:
            self.write_log("Decision rejected: position_size_zero")
            return

        if self._pending_entry is not None:
            self.write_log(
                "Decision rejected: one_active_open_intent (pending entry exists)"
            )
            return
        active_open = any(
            order.role.startswith("open") and order.active
            for order in self._order_state.active.values()
        )
        if active_open:
            self.write_log(
                "Decision rejected: one_active_open_intent (active open order exists)"
            )
            return

        self._pending_entry = PendingEntry(intent)
        direction_str = self._get_signal_direction(signal)
        order_price = self._execution_price(signal.price, signal.target_position)
        if direction_str == "long":
            order_ids = self._send_live_or_shadow(
                "open_long",
                lambda: self.buy(order_price, planned_size),
                price=order_price,
                volume=planned_size,
                closing=False,
                reason=intent.event_id,
            )
        else:
            order_ids = self._send_live_or_shadow(
                "open_short",
                lambda: self.short(order_price, planned_size),
                price=order_price,
                volume=planned_size,
                closing=False,
                reason=intent.event_id,
            )
        if not order_ids:
            self._pending_entry = None
            return
        self._order_state.submit(
            order_ids,
            role="open",
            price=order_price,
            volume=planned_size,
            reason=intent.event_id,
            timeout_at=self._order_timeout_at(),
        )
        self.order_status = "pending_open"
        self._persist_runtime_state()
        self.write_log(
            f"ENTRY: {intent.bsp_type} grade={intent.grade} "
            f"dir={direction_str} @ {order_price:.1f}"
        )

    def _send_live_or_shadow(
        self,
        action: str,
        sender,
        *,
        price: float,
        volume: int | float,
        closing: bool,
        reason: str,
    ) -> list:
        gate = self._refresh_production_gate()
        if not closing:
            hard = self._evaluate_hard_risk(int(volume or 0))
            if not hard.allowed:
                self.order_status = "hard_risk_blocked"
                self.hard_risk_status = "BLOCKED"
                self.hard_risk_reasons = hard.reason_text
                self.write_log(
                    f"Open blocked by hard risk: {hard.reason_text}"
                )
                return []
            self.hard_risk_status = "armed"
            self.hard_risk_reasons = ""
        if getattr(self, "_recovery_required", False):
            if not closing:
                self.order_status = "recovery_required"
                self.write_log(
                    "Open blocked by P7.2 recovery: "
                    f"{self.recovery_reasons}"
                )
                return []
            self.write_log(
                "Risk-reducing close allowed during P7.2 recovery: "
                f"{self.recovery_reasons}"
            )
        elif self.shadow_mode:
            if not gate.ready or not self.production_ready:
                self.order_status = "blocked"
                self.write_log(
                    f"Shadow action blocked by initialization gate: {gate.reason_text}"
                )
                return []
            self.order_status = "shadow"
            self.write_log(
                f"SHADOW {action}: {self.vt_symbol} volume={volume} "
                f"price={price:.1f} reason={reason}"
            )
            return []
        elif not closing and not gate.ready:
            self.order_status = "blocked"
            self.write_log(f"Order blocked by production gate: {gate.reason_text}")
            return []
        elif closing and not gate.ready:
            self.write_log(
                "Risk-reducing close allowed while production gate is blocked: "
                f"{gate.reason_text}"
            )
        return sender()

    def _evaluate_hard_risk(self, planned_open_lots: int) -> OpenDecision:
        config = getattr(self, "_config", None)
        risk_cfg = getattr(config, "risk", None) if config is not None else None
        state = HardRiskState(
            vt_symbol=str(getattr(self, "vt_symbol", "")),
            planned_open_lots=max(1, int(planned_open_lots)),
            manual_halt=bool(getattr(self, "manual_halt", False)),
            minimum_equity=float(getattr(self, "minimum_equity", 0.0) or 0.0),
            account_equity=self._account_equity(),
            last_tick_time=getattr(self, "_last_tick_time", None),
            last_account_time=getattr(self, "_last_account_time", None),
            tick_timeout_seconds=120.0,
            account_timeout_seconds=30.0,
            realized_day_loss=(
                self._risk_day_loss()
                if getattr(self, "_risk", None) is not None
                else None
            ),
            daily_loss_limit=(
                getattr(risk_cfg, "daily_loss_limit", None)
                if risk_cfg is not None
                else None
            ),
            drawdown_pct=(
                self._risk_drawdown_pct()
                if getattr(self, "_risk", None) is not None
                else None
            ),
            max_drawdown_pct=(
                getattr(risk_cfg, "max_drawdown_pct", None)
                if risk_cfg is not None
                else None
            ),
            active_main_contracts=self._active_main_contracts(),
            require_main_contract_confirm=not bool(getattr(self, "shadow_mode", True)),
        )
        decision = evaluate_open_guard(state)
        self.hard_risk_status = "armed" if decision.allowed else "BLOCKED"
        self.hard_risk_reasons = decision.reason_text
        last_tick = getattr(self, "_last_tick_time", None)
        last_account = getattr(self, "_last_account_time", None)
        self.last_tick_time_text = (
            last_tick.isoformat(timespec="seconds") if last_tick is not None else ""
        )
        self.last_account_time_text = (
            last_account.isoformat(timespec="seconds")
            if last_account is not None
            else ""
        )
        return decision

    def _risk_day_loss(self) -> float | None:
        risk = getattr(self, "_risk", None)
        if risk is None:
            return None
        state = getattr(risk, "get_state", lambda: {})()
        return float(state.get("daily_realized") or 0.0)

    def _active_main_contracts(self) -> tuple[str, ...]:
        raw = str(getattr(self, "active_main_contracts", "") or "").strip()
        if not raw:
            return ()
        return tuple(
            value.strip()
            for value in raw.split(",")
            if value.strip()
        )

    def _risk_drawdown_pct(self) -> float | None:
        risk = getattr(self, "_risk", None)
        if risk is None:
            return None
        state = getattr(risk, "get_state", lambda: {})()
        peak = float(state.get("peak_equity") or 0.0)
        equity = self._account_equity()
        if peak <= 0 or equity is None:
            return None
        return (peak - equity) / peak

    def _execution_price(self, price: float, quantity_delta: int) -> float:
        config = getattr(self, "_config", None)
        execution = config.execution if config is not None else None
        return adverse_fill_price(
            price,
            quantity_delta=quantity_delta,
            slippage_points=(execution.slippage_points if execution else 0.0),
            price_tick=(execution.price_tick if execution else 1.0),
        )

    def _order_timeout_at(self) -> str:
        """ISO deadline for a fresh order (default 30s), used by on_timer."""
        from datetime import timedelta

        seconds = int(getattr(self, "order_timeout_seconds", 30) or 30)
        return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def _account_equity(self) -> float | None:
        config = getattr(self, "_config", None)
        sizing = getattr(config, "sizing", None) if config is not None else None
        fallback = (
            float(getattr(sizing, "capital", None)) if sizing is not None else None
        )
        main_engine = getattr(getattr(self, "cta_engine", None), "main_engine", None)
        if main_engine is None:
            return fallback
        try:
            oms = main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            balances = [float(account.balance) for account in accounts]
            return sum(balances) if balances else fallback
        except Exception:
            return fallback

    def _account_available_funds(self) -> float | None:
        fallback = self._account_equity()
        main_engine = getattr(getattr(self, "cta_engine", None), "main_engine", None)
        if main_engine is None:
            return fallback
        try:
            oms = main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            available = [float(account.available) for account in accounts]
            return sum(available) if available else fallback
        except Exception:
            return fallback

    def _update_atr(self, bar: BarData) -> float | None:
        high = float(bar.high_price)
        low = float(bar.low_price)
        close = float(bar.close_price)
        tr = high - low
        if self._previous_close is not None:
            tr = max(tr, abs(high - self._previous_close), abs(low - self._previous_close))
        self._previous_close = close
        self._true_ranges.append(tr)
        period = self._config.sizing.atr_period if self._config else 20
        if len(self._true_ranges) > period:
            self._true_ranges = self._true_ranges[-period:]
        if len(self._true_ranges) < period:
            return None
        return sum(self._true_ranges) / period

    def _snapshot_production_ready(self) -> bool:
        """Authoritative readiness from the private snapshot (not GUI vars).

        VeighNa can overwrite production_ready via disk variable restore; the
        private _warmup_readiness snapshot is the only order-safety truth.
        """
        snapshot = getattr(self, "_warmup_readiness", None)
        if snapshot is None:
            return bool(self.production_ready)
        if snapshot.ready:
            return self.initialization_status not in {
                "shadow_only_stale",
                "market_data_blocked",
                "failed",
            }
        return False

    def _refresh_production_gate(self):
        self._ensure_runtime_state_helpers()
        live_risk_status = self._query_live_risk_status()
        gate = evaluate_production_gate(
            config=self._config,
            kl_window=int(self.kl_window),
            production_ready=self._snapshot_production_ready(),
            operator_confirmed=bool(self.operator_confirmed),
            shadow_mode=bool(self.shadow_mode),
            forward_confirmed=bool(self.forward_confirmed),
            risk_manager_confirmed=bool(self.risk_manager_confirmed),
            risk_manager_setting_path=self.risk_manager_setting_path,
            pending_order_count=self._order_state.active_count,
            live_risk_status=live_risk_status,
        )
        self._production_gate = gate
        if self.initialization_status in {
            "shadow_only_stale",
            "market_data_blocked",
            "failed",
        }:
            self.production_status = self.initialization_status
        else:
            self.production_status = gate.mode if gate.ready else "blocked"
        reasons = [self.initialization_error, gate.reason_text]
        self.production_block_reason = ";".join(reason for reason in reasons if reason)
        return gate

    def _query_live_risk_status(self):
        """Query the actual RiskEngine state from the live MainEngine."""
        from vnpy_chan.production_guard import inspect_live_risk_engine

        main_engine = getattr(getattr(self, "cta_engine", None), "main_engine", None)
        if main_engine is None:
            return None
        return inspect_live_risk_engine(main_engine)

    def _persist_runtime_state(self) -> None:
        self._ensure_runtime_state_helpers()
        self.order_state_json = self._order_state.to_json()
        risk = getattr(self, "_risk", None)
        if risk is not None:
            self.risk_state_json = json.dumps(
                risk.get_state(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        position_payload = _position_context_to_dict(
            getattr(self, "_position_context", None)
        )
        self.position_state_json = json.dumps(
            position_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.runtime_state_json = self._build_runtime_state_package(
            position_payload
        ).to_json()

    def _build_runtime_state_package(
        self,
        position_payload: dict[str, Any] | None,
    ) -> RuntimeStatePackage:
        decision_ids: list[str] = []
        kernel = getattr(self, "_decision_kernel", None)
        if kernel is not None:
            decision_ids = [
                str(record.decision_id) for record in kernel.decision_trace
            ]
        order_state = (
            json.loads(self.order_state_json) if self.order_state_json else {}
        )
        return RuntimeStatePackage(
            strategy_name=self.__class__.__name__,
            vt_symbol=getattr(self, "vt_symbol", ""),
            config_sha256=self._config_sha256(),
            position=position_payload,
            exit_state=self._exit_state_snapshot(),
            risk=json.loads(self.risk_state_json) if self.risk_state_json else {},
            orders=order_state,
            decision_ids=decision_ids,
            trade_ids=self._trade_ids(),
            vt_tradeids=sorted(self._order_state.consumed_trade_ids),
        )

    def _config_sha256(self) -> str:
        config = getattr(self, "_config", None)
        if config is None:
            return ""
        path = getattr(config, "source_path", "") or ""
        if path:
            import hashlib
            from pathlib import Path

            p = Path(path)
            if p.is_file():
                return hashlib.sha256(p.read_bytes()).hexdigest()
        return ""

    def _runtime_strategy_id(self) -> str:
        symbol = getattr(self, "vt_symbol", "")
        return f"{self.__class__.__name__}|{symbol}"

    def _trade_ids(self) -> list[str]:
        if not hasattr(self, "_order_state"):
            return []
        return [str(order.vt_orderid) for order in self._order_state.history]

    def _exit_state_snapshot(self) -> dict[str, Any]:
        manager = getattr(self, "_exit_manager", None)
        if manager is None:
            return {}
        try:
            snapshot = getattr(manager, "snapshot_state", None)
            if callable(snapshot):
                return dict(snapshot())
        except Exception:
            pass
        return {}

    def _restore_runtime_state(self) -> None:
        self._ensure_runtime_state_helpers()
        if self._runtime_state_restored:
            return
        self._runtime_state_restored = True

        if self.runtime_state_json:
            try:
                package = RuntimeStatePackage.from_json(
                    self.runtime_state_json,
                    expected_strategy_name=self.__class__.__name__,
                    expected_vt_symbol=getattr(self, "vt_symbol", ""),
                    expected_config_sha256=self._config_sha256(),
                )
                if package.position is not None and not self.position_state_json:
                    self.position_state_json = json.dumps(
                        package.position,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                if package.risk and not self.risk_state_json:
                    self.risk_state_json = json.dumps(
                        package.risk,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                if package.orders and not self.order_state_json:
                    self.order_state_json = json.dumps(
                        package.orders,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                # Persist vt_tradeids for post-restore merge (H1 fix: the
                # order_state_json rebuild would overwrite consumed ids).
                self._restored_vt_tradeids = list(package.vt_tradeids or [])
                # Also seed the current machine immediately so that the
                # dedup is already in effect before any order_state restore.
                if package.vt_tradeids:
                    self._order_state.consumed_trade_ids.update(package.vt_tradeids)
                self._restored_exit_state = dict(package.exit_state or {})
                self._restored_decision_ids = list(package.decision_ids or [])
                self._restored_trade_ids = list(package.trade_ids or [])
            except (RuntimeStateVersionError, ValueError, TypeError) as exc:
                self._accumulate_recovery(
                    f"runtime_state_invalid:{getattr(exc, 'reason', type(exc).__name__)}"
                )
                return

        if self.order_state_json:
            try:
                self._order_state = CtaOrderStatusMachine.from_json(
                    self.order_state_json,
                )
                self.order_status = self._order_state.summary
            except Exception as exc:
                self.order_status = "recovery_error"
                self._accumulate_recovery(f"order_state_invalid:{type(exc).__name__}")
                self.write_log(f"Order state restore failed: {exc}")
        # P7-R9: merge vt_tradeids AFTER order_state restore so that any
        # consumed ids from the runtime package survive an empty order_state.
        if (
            hasattr(self, "_restored_vt_tradeids")
            and self._restored_vt_tradeids
        ):
            self._order_state.consumed_trade_ids.update(self._restored_vt_tradeids)
        if self.risk_state_json and self._risk is not None:
            try:
                self._risk.load_state(json.loads(self.risk_state_json))
                self.risk_status = "restored"
            except Exception as exc:
                self.risk_status = "restore_error"
                self._accumulate_recovery(f"risk_state_invalid:{type(exc).__name__}")
                self.write_log(f"Risk state restore failed: {exc}")
        if self.position_state_json:
            try:
                restored = _position_context_from_dict(
                    json.loads(self.position_state_json)
                )
                self._position_context = restored
                if restored is not None and self._exit_manager is not None:
                    self._exit_manager.on_position_opened(restored)
                    self._restore_exit_manager_state()
            except Exception as exc:
                self._accumulate_recovery(f"position_state_invalid:{type(exc).__name__}")
                self.write_log(f"Position state restore failed: {exc}")

    def _restore_exit_manager_state(self) -> None:
        """Apply the restored exit_state to the ExitManager after re-open.

        Uses the public snapshot/restore contract added for P7-R3; the
        strategy never touches ExitManager private fields directly.
        """
        manager = getattr(self, "_exit_manager", None)
        payload = getattr(self, "_restored_exit_state", None)
        if manager is None or not payload:
            return
        try:
            restore = getattr(manager, "restore_state", None)
            if callable(restore):
                restore(dict(payload))
        except Exception as exc:
            self.write_log(f"ExitManager state restore failed: {exc}")

    def _accumulate_recovery(self, reason: str) -> None:
        """Accumulate a hard recovery reason; never cleared by later ALIGNED."""
        self._recovery_required = True
        current = self.recovery_reasons or ""
        reasons = [r for r in current.split(";") if r]
        if reason not in reasons:
            reasons.append(reason)
        self.recovery_reasons = ";".join(reasons)
        self.recovery_status = "RECOVERY_REQUIRED"

    def _check_oms_reconciliation(self) -> None:
        """Query OMS actual positions/orders and enter recovery if mismatched.

        Three-way comparison: self.pos vs PositionContext vs OMS, filtered by
        symbol/exchange/gateway.  OMS absent or unqueryable fails closed, and
        this never clears hard recovery reasons accumulated earlier.

        P7-R10: resolve exchange and gateway from structured vt_symbol parsing
        and MainEngine.get_contract so that positions/orders from other
        exchanges or gateways never affect this strategy's reconciliation.
        """
        self._ensure_runtime_state_helpers()
        main_engine = getattr(self.cta_engine, "main_engine", None)
        vt_symbol = str(getattr(self, "vt_symbol", ""))
        if not vt_symbol or "." not in vt_symbol:
            self._accumulate_recovery(
                f"oms_invalid_vt_symbol:{vt_symbol or 'empty'}"
            )
            return

        symbol, exchange = self._parse_vt_symbol(vt_symbol)
        gateway = self._resolve_gateway(main_engine, vt_symbol)

        if not exchange:
            self._accumulate_recovery("oms_exchange_unknown")
            return
        if main_engine is not None and not gateway:
            self._accumulate_recovery("oms_gateway_unknown")
            return

        if main_engine is None:
            self._accumulate_recovery("oms_unavailable:main_engine")
            return
        try:
            oms = main_engine.get_engine("oms")
        except Exception as exc:
            self._accumulate_recovery(f"oms_query_error:{type(exc).__name__}")
            return
        if oms is None:
            self._accumulate_recovery("oms_unavailable:no_oms_engine")
            return
        try:
            positions = oms.get_all_positions()
            orders = oms.get_all_active_orders()
        except Exception as exc:
            self._accumulate_recovery(f"oms_query_error:{type(exc).__name__}")
            return

        result = reconcile_oms(
            oms_positions=positions or [],
            oms_orders=orders or [],
            symbol=symbol,
            self_pos=int(getattr(self, "pos", 0) or 0),
            strategy_context=getattr(self, "_position_context", None),
            strategy_active_order_ids=tuple(self._order_state.active.keys()),
            exchange=exchange,
            gateway=gateway,
        )
        self._recovery_result = result
        if result.recovery_required:
            self._accumulate_recovery(result.reason_text)
        else:
            # Only a fresh aligned result on a clean (non-accumulated) state
            # may clear recovery.  Hard reasons from restore/query survive.
            if not self.recovery_reasons:
                self._recovery_required = False
                self.recovery_status = "ALIGNED"
            self.recovery_reasons = result.reason_text or self.recovery_reasons
        self.write_log(
            f"OMS reconciliation status={self.recovery_status} "
            f"oms_pos={result.oms_net_position} "
            f"self_pos={result.self_net_position} "
            f"strategy_pos={result.strategy_net_position} "
            f"oms_active={len(result.oms_active_orders)} "
            f"strategy_active={len(result.strategy_active_orders)}"
        )

    def _ensure_runtime_state_helpers(self) -> None:
        if not hasattr(self, "_order_state"):
            self._order_state = CtaOrderStatusMachine()
        if not hasattr(self, "_runtime_state_restored"):
            self._runtime_state_restored = False
        if not hasattr(self, "_recovery_required"):
            self._recovery_required = False
        if not hasattr(self, "_recovery_result"):
            self._recovery_result = None
        if not hasattr(self, "_last_tick_time"):
            self._last_tick_time = None
        if not hasattr(self, "_last_account_time"):
            self._last_account_time = None
        if not hasattr(self, "recovery_reasons"):
            self.recovery_reasons = ""
        if not hasattr(self, "recovery_status"):
            self.recovery_status = "not_checked"
        if not hasattr(self, "_restored_exit_state"):
            self._restored_exit_state = {}
        if not hasattr(self, "_restored_decision_ids"):
            self._restored_decision_ids = []
        if not hasattr(self, "_restored_trade_ids"):
            self._restored_trade_ids = []

    @staticmethod
    def _parse_vt_symbol(vt_symbol: str) -> tuple[str, str]:
        """Parse 'RB2610.SHFE' → ('RB2610', 'SHFE').

        Normalises Enum.value to plain string for consistent comparison.
        """
        part = vt_symbol.rsplit(".", 1)
        symbol = part[0]
        exchange = part[1] if len(part) == 2 else ""
        # Canonicalize: strip Enum wrapper if caller passed raw Exchange object value
        if hasattr(exchange, "value"):
            exchange = exchange.value
        return symbol.upper(), exchange.upper()

    def _resolve_gateway(self, main_engine, vt_symbol: str) -> str:
        """Resolve gateway_name from MainEngine for a vt_symbol.

        Returns empty string if unresolvable.
        """
        if main_engine is None:
            return ""
        try:
            contract = main_engine.get_contract(vt_symbol)
        except Exception:
            return ""
        if contract is None:
            return ""
        return str(getattr(contract, "gateway_name", "") or "")

    @property
    def position_context(self) -> PositionContext | None:
        return self._position_context

    @property
    def decision_trace(self) -> tuple[DecisionTraceRecord, ...]:
        if self._decision_kernel is None:
            return ()
        return self._decision_kernel.decision_trace

    @property
    def decomposition_state(self):
        if self._decision_kernel is None:
            return None
        return self._decision_kernel.decomposition_state

    @property
    def decomposition_transitions(self):
        if self._decision_kernel is None:
            return ()
        return self._decision_kernel.decomposition_transitions

    @property
    def warmup_readiness(self) -> WarmupReadiness:
        return self._warmup_readiness

    # ============================================================
    # GUI variable update
    # ============================================================

    def _update_gui_vars(self):
        """Refresh vnpy GUI monitoring variables."""
        try:
            kl = self._chan[0]
            self.bi_count = len(kl.bi_list) if kl.bi_list else 0
            self.seg_count = len(kl.seg_list.lst) if kl.seg_list else 0
            self.zs_count = len(kl.zs_list.zs_lst) if kl.zs_list else 0

            latest = self._chan.get_latest_bsp(idx=0, number=1)
            if latest and latest[0]:
                self.last_bsp_type = latest[0].type2str()
                self.last_bsp_direction = "buy" if latest[0].is_buy else "sell"
        except Exception:
            pass

    def _get_signal_direction(self, signal) -> str:
        """Infer direction from the signed target position."""
        if signal.target_position > 0:
            return "long"
        if signal.target_position < 0:
            return "short"
        raise ValueError("开仓目标仓位不能为 0")


def _confirmed_opposite(intent: TradeIntent | None, position: int) -> bool:
    if intent is None or intent.event is None or position == 0:
        return False
    if intent.event.state != SignalState.CONFIRMED:
        return False
    return (
        position > 0 and intent.event.direction == SignalDirection.SHORT
    ) or (position < 0 and intent.event.direction == SignalDirection.LONG)


def _timestamp_naive(value: pd.Timestamp) -> pd.Timestamp:
    if value.tzinfo is not None:
        return value.tz_convert("Asia/Shanghai").tz_localize(None)
    return value


def _bar_timestamp(bar: BarData) -> pd.Timestamp:
    return _timestamp_naive(pd.Timestamp(bar.datetime))


def _last_datetime(values: list[pd.Timestamp]) -> datetime | None:
    return values[-1].to_pydatetime() if values else None


def _position_context_to_dict(context: PositionContext | None) -> dict:
    if context is None:
        return {}
    return {
        "direction": context.direction.value,
        "entry_price": context.entry_price,
        "entry_time": str(context.entry_time) if context.entry_time is not None else "",
        "entry_bar": context.entry_bar,
        "volume": context.volume,
        "entry_grade": context.entry_grade,
        "event_id": context.event_id,
        "signal_key": context.signal_key,
        "decision_id": context.decision_id,
        "policy_id": context.policy_id,
        "bsp_type": context.bsp_type,
        "active_symbol": context.active_symbol,
        "bi_begin_price": context.bi_begin_price,
        "zs_high": context.zs_high,
        "zs_low": context.zs_low,
        "setup_invalidation_price": context.setup_invalidation_price,
        "execution_stop_price": context.execution_stop_price,
    }


def _position_context_from_dict(data: dict) -> PositionContext | None:
    if not data:
        return None
    return PositionContext(
        direction=SignalDirection(data["direction"]),
        entry_price=float(data["entry_price"]),
        entry_time=data.get("entry_time") or None,
        entry_bar=data.get("entry_bar"),
        volume=int(data["volume"]),
        entry_grade=str(data.get("entry_grade", "standard")),
        event_id=str(data.get("event_id", "")),
        signal_key=str(data.get("signal_key", "")),
        decision_id=str(data.get("decision_id", "")),
        policy_id=str(data.get("policy_id", "")),
        bsp_type=str(data.get("bsp_type", "")),
        active_symbol=data.get("active_symbol"),
        bi_begin_price=data.get("bi_begin_price"),
        zs_high=data.get("zs_high"),
        zs_low=data.get("zs_low"),
        setup_invalidation_price=data.get("setup_invalidation_price"),
        execution_stop_price=data.get("execution_stop_price"),
    )
