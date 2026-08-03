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
from signal_core import SignalDirection, SignalState
from strategy_policy.position import PositionContext
from vnpy_chan.order_state import CtaOrderStatusMachine
from vnpy_chan.production_guard import (
    DEFAULT_RISK_MANAGER_SETTING_PATH,
    evaluate_production_gate,
)
from vnpy_chan.session_aggregator import RbSessionBarAggregator, SUPPORTED_WINDOWS

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
    load_days = 30
    production_ready = False
    shadow_mode = True
    forward_confirmed = False
    risk_manager_confirmed = False
    risk_manager_setting_path = str(DEFAULT_RISK_MANAGER_SETTING_PATH)

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
    position_state_json = ""
    risk_state_json = ""
    order_state_json = ""

    parameters = [
        "chan_project_path",
        "config_yaml",
        "kl_window",
        "fixed_size",
        "load_days",
        "production_ready",
        "shadow_mode",
        "forward_confirmed",
        "risk_manager_confirmed",
        "risk_manager_setting_path",
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
        "position_state_json",
        "risk_state_json",
        "order_state_json",
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
        self.bg = BarGenerator(self.on_bar)
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
        self._order_state = CtaOrderStatusMachine()
        self._runtime_state_restored = False

        self._position_context: PositionContext | None = None
        self._pending_entry: PendingEntry | None = None
        self._pending_reversal: PendingEntry | None = None
        self._trade_pnl_batch: list[float] = []
        self._previous_close: float | None = None
        self._true_ranges: list[float] = []

    # ============================================================
    # vnpy Lifecycle
    # ============================================================

    def on_init(self):
        """Initialize: load historical bars and warm up CChan."""
        self.write_log(f"ChanBspStrategy init: {self.vt_symbol}")
        self._init_chan_pipeline()
        gate = self._refresh_production_gate()
        if not gate.ready and not self.shadow_mode:
            message = f"Production gate blocked: {gate.reason_text}"
            self.write_log(message)
            raise RuntimeError(message)

        if self.load_days > 0:
            self.write_log(f"Loading {self.load_days} days of historical bars...")
            self.load_bar(self.load_days, interval=Interval.MINUTE)

        cfg_info = ""
        if self._config:
            cfg_info = (
                f" grade={self._config.grading.min_grade.value}"
                f" exits={[e.type for e in self._config.exits]}"
            )
        self.write_log(f"ChanBspStrategy ready.{cfg_info}")

    def on_start(self):
        """Start trading."""
        self._restore_runtime_state()
        gate = self._refresh_production_gate()
        self.write_log(
            f"ChanBspStrategy started mode={gate.mode} "
            f"reason={gate.reason_text or 'ready'}"
        )

    def on_stop(self):
        """Stop trading."""
        self.write_log(
            f"ChanBspStrategy stopped: "
            f"trades={self.total_trades} pnl={self.total_pnl:.0f}pts"
        )

    # ============================================================
    # Bar handling
    # ============================================================

    def on_tick(self, tick: TickData):
        """Tick -> BarGenerator."""
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """1-min bar -> BarGenerator -> N-min bar."""
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
            self._on_kl_bar(bar)
            return
        kl_bar = self._kl_aggregator.update_bar(bar)
        if kl_bar is not None:
            self._on_kl_bar(kl_bar)

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

    def _on_kl_bar(self, bar: BarData):
        """N-minute bar completed. Main strategy logic."""
        self.bars_processed += 1

        if self._chan is None or self._decision_kernel is None:
            return

        # 1. Feed bar to CChan
        timestamp = self._window_end_time(bar, self._current_kl_type())
        klu = self._bar_to_klu(bar, timestamp=timestamp)
        self._chan.trigger_load({klu.kl_type: [klu]})

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
            current_position=int(self.pos),
            price=price,
            timestamp=timestamp,
            active_symbol=bar.symbol,
            lv_idx=0,
            account_equity=self._account_equity(),
            available_funds=self._account_available_funds(),
            atr=atr,
        )

        # 4. Check exit rules, including confirmed opposite BSP and chan momentum.
        if self.pos != 0 and self._exit_manager is not None:
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

        self.put_event()

    # ============================================================
    # Order/Trade callbacks
    # ============================================================

    def on_trade(self, trade: TradeData):
        """Track PnL on close."""
        self._ensure_runtime_state_helpers()
        self._order_state.on_trade(trade)
        self.order_status = self._order_state.summary
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
        if not tracked.active and tracked.role.startswith("open") and tracked.traded <= 0:
            self._pending_entry = None
        self._persist_runtime_state()
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        self._ensure_runtime_state_helpers()
        self._order_state.on_stop_order(stop_order)
        self.order_status = self._order_state.summary
        self._persist_runtime_state()
        self.put_event()

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
        return RbSessionBarAggregator(int(window))

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
        """Close current position."""
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
            self.order_status = "pending_close"
            self._persist_runtime_state()

    def _submit_open_intent(self, intent: TradeIntent) -> None:
        signal = intent.signal
        planned_size = abs(signal.target_position)
        if planned_size <= 0:
            self.write_log("Decision rejected: position_size_zero")
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
        if self.shadow_mode:
            self.order_status = "shadow"
            self.write_log(
                f"SHADOW {action}: {self.vt_symbol} volume={volume} "
                f"price={price:.1f} reason={reason}"
            )
            return []
        if not closing and not gate.ready:
            self.order_status = "blocked"
            self.write_log(f"Order blocked by production gate: {gate.reason_text}")
            return []
        if closing and not gate.ready:
            self.write_log(
                "Risk-reducing close allowed while production gate is blocked: "
                f"{gate.reason_text}"
            )
        return sender()

    def _execution_price(self, price: float, quantity_delta: int) -> float:
        config = getattr(self, "_config", None)
        execution = config.execution if config is not None else None
        return adverse_fill_price(
            price,
            quantity_delta=quantity_delta,
            slippage_points=(execution.slippage_points if execution else 0.0),
            price_tick=(execution.price_tick if execution else 1.0),
        )

    def _account_equity(self) -> float | None:
        fallback = self._config.sizing.capital if self._config else None
        main_engine = getattr(self.cta_engine, "main_engine", None)
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
        main_engine = getattr(self.cta_engine, "main_engine", None)
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

    def _refresh_production_gate(self):
        self._ensure_runtime_state_helpers()
        gate = evaluate_production_gate(
            config=self._config,
            kl_window=int(self.kl_window),
            production_ready=bool(self.production_ready),
            shadow_mode=bool(self.shadow_mode),
            forward_confirmed=bool(self.forward_confirmed),
            risk_manager_confirmed=bool(self.risk_manager_confirmed),
            risk_manager_setting_path=self.risk_manager_setting_path,
            pending_order_count=self._order_state.active_count,
        )
        self._production_gate = gate
        self.production_status = gate.mode if gate.ready else "blocked"
        self.production_block_reason = gate.reason_text
        return gate

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
        self.position_state_json = json.dumps(
            _position_context_to_dict(getattr(self, "_position_context", None)),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _restore_runtime_state(self) -> None:
        self._ensure_runtime_state_helpers()
        if self._runtime_state_restored:
            return
        self._runtime_state_restored = True
        if self.order_state_json:
            try:
                self._order_state = CtaOrderStatusMachine.from_json(
                    self.order_state_json,
                )
                self.order_status = self._order_state.summary
            except Exception as exc:
                self.order_status = "recovery_error"
                self.write_log(f"Order state restore failed: {exc}")
        if self.risk_state_json and self._risk is not None:
            try:
                self._risk.load_state(json.loads(self.risk_state_json))
                self.risk_status = "restored"
            except Exception as exc:
                self.risk_status = "restore_error"
                self.write_log(f"Risk state restore failed: {exc}")
        if self.position_state_json:
            try:
                restored = _position_context_from_dict(
                    json.loads(self.position_state_json)
                )
                self._position_context = restored
                if restored is not None and self._exit_manager is not None:
                    self._exit_manager.on_position_opened(restored)
            except Exception as exc:
                self.write_log(f"Position state restore failed: {exc}")

    def _ensure_runtime_state_helpers(self) -> None:
        if not hasattr(self, "_order_state"):
            self._order_state = CtaOrderStatusMachine()
        if not hasattr(self, "_runtime_state_restored"):
            self._runtime_state_restored = False

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
