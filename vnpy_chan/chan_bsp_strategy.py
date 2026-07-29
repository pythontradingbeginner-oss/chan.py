# -*- coding: utf-8 -*-
"""ChanBspStrategy: Chan Theory BSP Trend Strategy as vnpy CtaTemplate.

This is a thin adapter that wraps the entire chan.py signal pipeline
inside a standard CtaTemplate so it appears in vnpy's strategy dropdown.

Flow per N-minute bar:
  BarData -> CKLine_Unit -> CChan.trigger_load()
  -> GradedChanStrategy.on_bar() -> grade filter -> signal
  -> ExitManager.check() (if position held)
  -> RiskManager.approve()
  -> vnpy buy/sell/short/cover

Place this file in: C:\\Users\\Administrator\\strategies\\
Ensure chan.py project is in PYTHONPATH.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
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
from vnpy.trader.constant import Direction, Interval, Offset

# Ensure chan.py project is in sys.path
_CHAN_CANDIDATES = [
    Path("H:/Github/chan.py"),
    Path.home() / "Github" / "chan.py",
    Path(__file__).resolve().parent.parent,
]
for _cp in _CHAN_CANDIDATES:
    if _cp.exists() and str(_cp) not in sys.path:
        sys.path.insert(0, str(_cp))

# Config defaults
DEFAULT_CHAN_PROJECT = "H:/Github/chan.py"
DEFAULT_CONFIG_YAML = "configs/rb_15m_trend_ideal.yaml"


class ChanBspStrategy(CtaTemplate):
    """Chan Theory BSP Trend Strategy (CtaTemplate adapter).

    Triggers on each N-minute bar:
      1. bar -> CKLine_Unit -> CChan.trigger_load()
      2. GradedChanStrategy.on_bar() -> grade filter -> signal
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

    parameters = [
        "chan_project_path",
        "config_yaml",
        "kl_window",
        "fixed_size",
        "load_days",
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
    ]

    # --- Constructor ---

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # Ensure project path is importable
        for p in [self.chan_project_path, DEFAULT_CHAN_PROJECT]:
            if p and Path(p).exists() and str(p) not in sys.path:
                sys.path.insert(0, str(p))

        # BarGenerator for N-minute bar synthesis
        self.bg = BarGenerator(self.on_bar, self.kl_window, self._on_kl_bar)

        # Lazy-initialized chan components
        self._chan = None
        self._wrapper = None
        self._extractor = None
        self._exit_manager = None
        self._risk = None
        self._config = None

        # Position tracking
        self._entry_price = 0.0
        self._entry_grade = ""
        self._entry_direction_str = ""
        self._entry_bar = 0
        self._trade_pnl_batch: list[float] = []

    # ============================================================
    # vnpy Lifecycle
    # ============================================================

    def on_init(self):
        """Initialize: load historical bars and warm up CChan."""
        self.write_log(f"ChanBspStrategy init: {self.vt_symbol}")
        self._init_chan_pipeline()

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
        self.write_log("ChanBspStrategy started")

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
        self.bg.update_bar(bar)

    def _on_kl_bar(self, bar: BarData):
        """N-minute bar completed. Main strategy logic."""
        self.bars_processed += 1

        if self._chan is None or self._wrapper is None:
            return

        # 1. Feed bar to CChan
        klu = self._bar_to_klu(bar)
        self._chan.trigger_load({klu.kl_type: [klu]})

        price = float(bar.close_price)
        timestamp = (
            bar.datetime
            if isinstance(bar.datetime, datetime)
            else pd.Timestamp(bar.datetime)
        )

        # 2. Update GUI display
        self._update_gui_vars()

        # 3. Check exit rules
        if self.pos != 0 and self._exit_manager is not None:
            exit_sig = self._check_exit(bar, price, timestamp)
            if exit_sig is not None:
                self.exit_reason = exit_sig.reason_code
                self._close_existing(bar, exit_sig.reason_code)
                self.put_event()
                return

        # 4. Check entry signals
        if self.pos == 0:
            graded = self._wrapper.on_bar(
                chan=self._chan,
                current_position=0,
                price=price,
                timestamp=timestamp,
                active_symbol=bar.symbol,
                lv_idx=0,
                extractor=self._extractor,
            )
            if graded is not None:
                self.last_signal_grade = graded.grade

                # Risk check
                if self._risk is not None:
                    from chan_futures.strategy import StrategySignal
                    dummy_sig = StrategySignal(
                        timestamp=timestamp,
                        action="open_long",
                        target_position=1,
                        price=price,
                        reason="chan_bsp",
                        bsp_type=graded.bsp_type,
                        bsp_bi_idx=0,
                        bsp_klu_idx=0,
                        active_symbol=bar.symbol,
                    )
                    decision = self._risk.approve(dummy_sig)
                    if not decision.approved:
                        self.write_log(f"Risk rejected: {decision.reason}")
                        self.put_event()
                        return

                # Determine direction
                direction_str = self._get_signal_direction(graded)
                self._entry_price = price
                self._entry_grade = graded.grade
                self._entry_direction_str = direction_str
                self._entry_bar = self.bars_processed

                if direction_str == "long":
                    self.buy(price, self.fixed_size)
                elif direction_str == "short":
                    self.short(price, self.fixed_size)

                self.write_log(
                    f"ENTRY: {graded.bsp_type} grade={graded.grade} "
                    f"dir={direction_str} @ {price:.1f}"
                )

        # 5. Strategy reversal (opposite BSP)
        elif self.pos != 0:
            opposite = self._detect_opposite_signal()
            if opposite:
                self.exit_reason = "strategy_reverse"
                self._close_existing(bar, "strategy_reverse")

        self.put_event()

    # ============================================================
    # Order/Trade callbacks
    # ============================================================

    def on_trade(self, trade: TradeData):
        """Track PnL on close."""
        if self._entry_price > 0 and trade.offset == Offset.CLOSE:
            mult = 1 if self._entry_direction_str == "long" else -1
            pnl = mult * (trade.price - self._entry_price) - 2.0  # 2pt fee
            self.total_pnl += pnl
            self.total_trades += 1
            self._trade_pnl_batch.append(pnl)

            if self._risk is not None:
                self._risk.on_fill(pnl_points=pnl, fill_time=datetime.now())

            self.write_log(
                f"CLOSE: @ {trade.price:.1f} PnL={pnl:.0f}pts "
                f"cumulative={self.total_pnl:.0f}pts trades={self.total_trades}"
            )
            self._entry_price = 0.0
            self._entry_grade = ""
            self._entry_direction_str = ""
            self.exit_reason = ""

        self.put_event()

    def on_order(self, order: OrderData):
        pass

    def on_stop_order(self, stop_order: StopOrder):
        pass

    # ============================================================
    # Internal: initialize chan pipeline (lazy)
    # ============================================================

    def _init_chan_pipeline(self):
        """Import and construct all chan.py components."""
        try:
            from Chan import CChan
            from ChanConfig import CChanConfig
            from Common.CEnum import AUTYPE, KL_TYPE

            from chan_futures.config_loader import load_config, make_exit_manager
            from chan_futures.graded_strategy import (
                GradeFilterConfig,
                GradedChanStrategy,
            )
            from chan_futures.risk import RiskConfig, RiskManager
            from signal_core import SignalExtractor
        except ImportError as e:
            self.write_log(f"Import error: {e}. Check chan_project_path.")
            return

        # Load config
        config_path = Path(self.chan_project_path) / self.config_yaml
        if config_path.exists():
            self._config = load_config(str(config_path))
        else:
            from chan_futures.config import StrategyConfig
            self._config = StrategyConfig()

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

        # Graded strategy
        from chan_futures.backtest import _TYPE_STR_TO_BSP
        accepted = [
            _TYPE_STR_TO_BSP.get(t)
            for t in cfg.entry.get("accepted_bsp_types", ["1", "1p", "2"])
            if t in _TYPE_STR_TO_BSP
        ]
        self._wrapper = GradedChanStrategy(
            GradeFilterConfig(
                min_grade=cfg.grading.min_grade.value,
                accepted_bsp_types=accepted or None,
                allow_short=cfg.allow_short,
                require_confirmed_bsp=True,
            )
        )

        # SignalExtractor
        tf_str = kl_type.name.replace("K_", "").replace("M", "m")
        self._extractor = SignalExtractor(symbol="RB", timeframe=tf_str)

        # ExitManager
        self._exit_manager = make_exit_manager(cfg)

        # RiskManager
        self._risk = RiskManager(
            RiskConfig(
                max_abs_position=cfg.risk.max_abs_position,
                max_loss_points=cfg.risk.max_loss_points,
                daily_loss_limit=cfg.risk.daily_loss_limit,
                max_consecutive_losses=cfg.risk.max_consecutive_losses,
            )
        )

    # ============================================================
    # Internal: bar conversion
    # ============================================================

    def _bar_to_klu(self, bar: BarData):
        """Convert vnpy BarData to chan.py CKLine_Unit."""
        from Common.CEnum import DATA_FIELD, KL_TYPE
        from Common.CTime import CTime
        from KLine.KLine_Unit import CKLine_Unit

        ts = pd.Timestamp(bar.datetime)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)

        w2k = {
            1: KL_TYPE.K_1M, 5: KL_TYPE.K_5M, 15: KL_TYPE.K_15M,
            30: KL_TYPE.K_30M, 60: KL_TYPE.K_60M,
        }
        kl_type = w2k.get(self.kl_window, KL_TYPE.K_15M)

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

    # ============================================================
    # Exit checking
    # ============================================================

    def _check_exit(self, bar: BarData, price: float, timestamp):
        """Run ExitManager.check() for current position."""
        if self._exit_manager is None or not self._exit_manager.is_active:
            return None

        from strategy_policy.exit_rules import SignalDirection as ExitSigDir
        direction = ExitSigDir.LONG if self.pos > 0 else ExitSigDir.SHORT

        return self._exit_manager.check(
            bar_end_time=timestamp,
            open=float(bar.open_price),
            high=float(bar.high_price),
            low=float(bar.low_price),
            close=price,
            opposite_signal_triggered=False,
            chan_snapshot=None,
        )

    def _close_existing(self, bar: BarData, reason: str):
        """Close current position."""
        if self.pos > 0:
            self.sell(bar.close_price, abs(self.pos))
        elif self.pos < 0:
            self.cover(bar.close_price, abs(self.pos))

    def _detect_opposite_signal(self) -> bool:
        """Check if latest BSP signals a strategy reversal."""
        try:
            latest = self._chan.get_latest_bsp(idx=0, number=1)
            if not latest or not latest[0]:
                return False
            bsp = latest[0]
            inner = self._wrapper._inner
            if inner._consumed_keys is not None:
                key = (bsp.bi.idx, bsp.klu.idx, bsp.type2str(), bsp.is_buy)
                if key in inner._consumed_keys:
                    return False
                inner._consumed_keys.add(key)
            if self.pos > 0 and not bsp.is_buy:
                return True
            if self.pos < 0 and bsp.is_buy:
                return True
        except Exception:
            pass
        return False

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

    def _get_signal_direction(self, graded) -> str:
        """Infer direction from GradedSignal action."""
        sig = graded.signal
        if "long" in sig.action:
            return "long"
        if "short" in sig.action:
            return "short"
        return "long"
