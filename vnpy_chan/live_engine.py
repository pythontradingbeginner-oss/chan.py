"""预发布事件引擎 —— 从 vn.py bar 驱动完整策略管线。

与回测 _run_loop 共享同一套:
  - GradedChanStrategy (入场信号 + grade 过滤)
  - ExitManager (出场规则)
  - RiskManager (风控)
  - SignalExtractor (信号提取)

仅执行层不同: 回测用 SimulatedExecutionEngine, 实盘用 VnpyExecutionEngine。

状态机:
  idle → waiting_signal → pending_open → in_position → closing → idle

研究/联调用法（不得作为当前生产宿主）:
    engine = LiveTradingEngine(main_engine, event_engine)
    engine.init_strategy(config)
    engine.start()  # 订阅 EVENT_BAR
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE
from vnpy.trader.object import BarData, OrderData, OrderRequest, TradeData

try:
    from vnpy.trader.event import EVENT_BAR
except ImportError:
    # vn.py core publishes ticks; bar-producing apps can publish this conventional topic.
    EVENT_BAR = "eBar."

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE
from signal_core import SignalExtractor, SignalDirection, SignalState
from signal_core.lifecycle import SignalJournal, SignalLifecycleTracker
from strategy_policy.exit_rules import (
    ExitManager,
    ExitSignal,
)
from chan_futures.config import StrategyConfig
from chan_futures.execution import adverse_fill_price
from chan_futures.config_loader import (
    load_config,
    make_exit_manager,
    make_runtime_decision_kernel,
)
from chan_futures.graded_strategy import GradedChanStrategy
from chan_futures.risk import CloseFillRecord, RiskConfig, RiskManager
from chan_futures.risk_session import RbRiskSessionResolver
from chan_futures.position_transition import decompose_position_transition
from chan_futures.option_d import probe_intent_identity
from chan_futures.trade_intent import DecisionTraceRecord, PendingEntry, TradeIntent
from data_foundation import RBTradingCalendar
from strategy_policy.position import PositionContext, position_id_from_open_fill_id

from .converter import bar_to_klu, window_to_kl_type
from .snapshot import ChanSnapshotManager


def _kl_type_minutes(value: str) -> int:
    mapping = {
        "K_1M": 1,
        "K_5M": 5,
        "K_15M": 15,
        "K_30M": 30,
        "K_60M": 60,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unsupported intraday K-line type: {value}") from exc


# ════════════════════════════════════════════════════════════════
# 引擎状态
# ════════════════════════════════════════════════════════════════

@dataclass
class _SignalContext:
    """待确认信号的追踪状态。"""

    signal_key: str
    first_seen: datetime
    last_seen: datetime
    direction: SignalDirection
    primary_bsp: str
    grade: str
    structural_score: float
    reference_price: float
    bi_begin_price: float | None = None
    zs_high: float | None = None
    zs_low: float | None = None
    bars_held: int = 0  # 自首次出现至今的 bar 数


# ════════════════════════════════════════════
# 主引擎
# ════════════════════════════════════════════


class LiveTradingEngine(BaseEngine):
    """预发布的有状态缠论事件引擎，不是当前生产下单宿主。

    当前生产宿主是 ``ChanBspStrategy``。本类保留信号接线和订单生命周期
    对照实现；在补齐与 CTA 相同的持久化、OMS 对账和恢复门禁前，不得用于
    生产下单。

    每根 bar 的决策流程:
      1. SnapshotManager 更新 CChan
      2. 如有活动仓位 → ExitManager.check() → 出场？
      3. 否则 → GradedChanStrategy.evaluate_bar() → SignalDecision
      4. 如果 (decision.accepted, 风控通过) → 发送开仓订单
      5. 记录所有信号到 SignalJournal
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, "ChanLive")
        self._config: StrategyConfig | None = None
        self._chan: CChan | None = None
        self._snapshot: ChanSnapshotManager | None = None
        self._wrapper: GradedChanStrategy | None = None
        self._extractor: SignalExtractor | None = None
        self._decision_kernel = None
        self._exit_manager: ExitManager | None = None
        self._risk: RiskManager | None = None
        self._journal: SignalJournal | None = None
        self._tracker: SignalLifecycleTracker | None = None
        self._calendar = RBTradingCalendar.load_default()
        self._risk_session_resolver = RbRiskSessionResolver(self._calendar)

        # ── 状态 ──
        self._position: PositionContext | None = None
        self._pending_signals: dict[str, _SignalContext] = {}
        self._bar_count: int = 0
        self._vt_symbol: str = ""

        # ── 订单追踪 ──
        self._pending_order_ids: set[str] = set()
        self._pending_entries: dict[str, PendingEntry] = {}
        self._terminal_reported_traded: dict[str, int] = {}
        self._pending_reversal: PendingEntry | None = None
        self._current_order_ref: str = ""
        self._previous_close: float | None = None
        self._true_ranges: list[float] = []

    # ════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════

    def init_strategy(
        self,
        config: StrategyConfig | str,
        *,
        symbol: str = "RB99",
        journal_dir: str | Path = "reports/live_signals",
    ) -> None:
        """加载策略配置并初始化所有组件。

        Args:
            config: StrategyConfig 或 YAML 路径
            symbol: vnpy 的 vt_symbol (e.g. "RB99.SHFE")
            journal_dir: 信号日志输出目录
        """
        if isinstance(config, str):
            self._config = load_config(config)
        else:
            self._config = config

        cfg = self._config
        self._vt_symbol = symbol
        kl_type = window_to_kl_type(_kl_type_minutes(cfg.kl_type))

        # ── CChan ──
        self._chan = CChan(
            code=symbol,
            begin_time=None,
            end_time=None,
            data_src="csv",
            lv_list=[kl_type],
            config=CChanConfig(cfg.chan.to_dict()),
            autype=AUTYPE.NONE,
        )

        # ── SnapshotManager ──
        self._snapshot = ChanSnapshotManager(
            chan=self._chan,
            lv_list=[kl_type],
        )

        # ── 三端共享决策管线 ──
        self._decision_kernel = make_runtime_decision_kernel(
            cfg,
            symbol="RB",
            timeframe=f"{_kl_type_minutes(cfg.kl_type)}m",
        )
        self._wrapper = self._decision_kernel.strategy
        self._extractor = self._decision_kernel.extractor

        # ── ExitManager ──
        self._exit_manager = make_exit_manager(cfg)

        # ── RiskManager ──
        initial_equity, equity_source, equity_scope = (
            self._account_equity_observation()
        )
        self._risk = RiskManager(
            RiskConfig(
                max_abs_position=cfg.risk.max_abs_position,
                max_loss_points=cfg.risk.max_loss_points,
                daily_loss_limit=cfg.risk.daily_loss_limit,
                max_consecutive_losses=cfg.risk.max_consecutive_losses,
                max_drawdown_pct=None,
                require_session_key=cfg.risk.daily_loss_limit is not None,
                profile=cfg.risk.profile,
                max_loss_points_mode=cfg.risk.max_loss_points_mode,
                daily_loss_limit_mode=cfg.risk.daily_loss_limit_mode,
                max_consecutive_losses_mode=cfg.risk.max_consecutive_losses_mode,
                max_drawdown_pct_mode=cfg.risk.max_drawdown_pct_mode,
            ),
            initial_equity=initial_equity,
            initial_equity_source=equity_source,
            equity_scope=equity_scope,
            max_drawdown_capability="disabled_by_design",
        )

        # ── SignalJournal ──
        self._journal = SignalJournal(journal_dir, format="csv")
        self._tracker = SignalLifecycleTracker()

        self._log_init(cfg)

    def _log_init(self, cfg: StrategyConfig) -> None:
        self.main_engine.write_log(
            f"[ChanLive] 策略已初始化: {cfg.code} {cfg.kl_type} "
            f"grade={cfg.grading.min_grade.value} "
            f"policy={cfg.entry.get('policy_mode', 'legacy')} "
            f"exits={[e.type for e in cfg.exits]} "
            f"risk={cfg.risk}",
            self.engine_name,
        )

    # ════════════════════════════════════════
    # 启动 / 停止
    # ════════════════════════════════════════

    def start(self, vt_symbol: str | None = None) -> None:
        """注册事件并开始接收 bar。"""
        if vt_symbol:
            self._vt_symbol = vt_symbol
        if not self._vt_symbol:
            raise ValueError("必须先设置 vt_symbol")

        self.event_engine.register(EVENT_BAR, self._on_bar)
        self.event_engine.register(EVENT_ORDER, self._on_order)
        self.event_engine.register(EVENT_TRADE, self._on_trade)
        self.main_engine.write_log(
            f"[ChanLive] 已启动, 监听 {self._vt_symbol}", self.engine_name
        )

    def stop(self) -> None:
        self.event_engine.unregister(EVENT_BAR, self._on_bar)
        self.event_engine.unregister(EVENT_ORDER, self._on_order)
        self.event_engine.unregister(EVENT_TRADE, self._on_trade)
        self.main_engine.write_log("[ChanLive] 已停止", self.engine_name)

    # ════════════════════════════════════════
    # 事件处理
    # ════════════════════════════════════════

    def _on_bar(self, event: Event) -> None:
        """每根 bar 的主循环。"""
        if (
            self._config is None
            or self._chan is None
            or self._snapshot is None
            or self._decision_kernel is None
        ):
            return

        bar: BarData = event.data
        vt_sym = getattr(bar, "vt_symbol", "")
        if self._vt_symbol and vt_sym != self._vt_symbol:
            return

        self._bar_count += 1
        klu = bar_to_klu(bar, kl_type=self._snapshot.lv_list[0])
        self._snapshot.feed(klu)

        price = float(bar.close_price)
        atr = self._update_atr(bar)
        timestamp = bar.datetime if isinstance(bar.datetime, datetime) else pd.Timestamp(bar.datetime)
        risk_session = None
        equity_value, equity_source, equity_scope = (
            self._account_equity_observation()
        )
        if self._risk is not None:
            risk_session = self._advance_risk_session(timestamp, source="live_bar")
            self._risk.observe_equity(
                equity_value,
                source=equity_source,
                scope=equity_scope,
                max_drawdown_capability="disabled_by_design",
            )
        chan_snap = self._snapshot.current
        if chan_snap is None:
            return
        self._decision_kernel.observe_structure(
            chan=chan_snap,
            timestamp=timestamp,
            lv_idx=0,
        )

        current_position = 0
        if self._position is not None:
            sign = 1 if self._position.direction == SignalDirection.LONG else -1
            current_position = sign * self._position.volume

        # ── 1. 先形成当根统一决策，供反向 BSP 出场使用 ──
        intent = self._decision_kernel.evaluate_bar(
            chan=chan_snap,
            current_position=current_position,
            price=price,
            timestamp=timestamp,
            active_symbol=bar.symbol,
            lv_idx=0,
            account_equity=equity_value,
            available_funds=self._account_available_funds(),
            atr=atr,
            risk_session_key=(risk_session.session_key if risk_session else None),
            risk_session_observed_at=(
                risk_session.observed_at if risk_session else None
            ),
            risk_session_source=(risk_session.source if risk_session else ""),
            equity_source=equity_source,
            equity_scope=equity_scope,
            max_drawdown_capability="disabled_by_design",
        )

        # ── 2. 按统一优先级检查出场规则 ──
        if self._position is not None and self._exit_manager is not None:
            exit_signal = self._check_exit(
                open=float(bar.open_price),
                high=float(bar.high_price),
                low=float(bar.low_price),
                close=price,
                bar_end_time=timestamp,
                intent=intent,
                atr=atr,
            )
            if exit_signal is not None:
                self._pending_reversal = None
                self._close_position(exit_signal.exit_price, exit_signal.reason_code, exit_signal.description)
                return

        # ── 3. 消费已经审计过的同一个 TradeIntent ──
        if intent is not None:
            self._record_signal(intent, timestamp)
            if not intent.accepted:
                self._log("决策拒绝: " + ",".join(intent.decision.reason_codes))
                return

            signal = intent.signal
            transition = decompose_position_transition(
                current_position,
                signal.target_position,
            )
            if transition.is_reversal:
                self._pending_reversal = PendingEntry(intent)
                self._close_position(signal.price, "strategy_reverse", "策略反转")
            elif transition.close_leg is not None:
                self._pending_reversal = None
                self._close_position(
                    signal.price,
                    "strategy_close",
                    "策略平仓",
                    volume=transition.close_leg.quantity,
                )
            elif transition.open_leg is not None:
                self._approve_and_open_position(intent, before_position=current_position)

        # ── 3. 信号状态更新 (pending signals → confirmed/invalidated) ──
        self._update_pending_signals(timestamp)

    def feed_parent_bar(self, bar: BarData) -> None:
        """Feed one completed parent bar before the same-time decision bar."""
        self._feed_context_bar(bar, parent=True)

    def feed_child_bar(self, bar: BarData) -> None:
        """Feed one completed child bar before the same-time decision bar."""
        self._feed_context_bar(bar, parent=False)

    def _feed_context_bar(self, bar: BarData, *, parent: bool) -> None:
        if self._config is None or self._decision_kernel is None:
            raise RuntimeError("strategy is not initialized")
        params = self._config.multi_level
        if not params.enabled:
            return
        level_name = params.parent_kl_type if parent else params.child_kl_type
        kl_type = window_to_kl_type(_kl_type_minutes(level_name))
        klu = bar_to_klu(bar, kl_type=kl_type)
        if parent:
            self._decision_kernel.observe_parent_bar(
                klu, available_at=bar.datetime
            )
        else:
            self._decision_kernel.observe_child_bar(
                klu, available_at=bar.datetime
            )

    def _on_order(self, event: Event) -> None:
        order: OrderData = event.data
        if order.vt_orderid not in self._pending_order_ids:
            return
        self._log(f"订单状态: {order.vt_orderid} → {order.status.value}")
        pending = self._pending_entries.get(order.vt_orderid)
        reported_fill_pending = bool(
            pending is not None
            and pending.probe_intent_id
            and int(getattr(order, "traded", 0) or 0) > pending.filled_volume
        )
        is_terminal = not bool(order.is_active())
        if is_terminal:
            if (
                pending is not None
                and pending.probe_intent_id
                and self._risk is not None
            ):
                self._risk.mark_probe_order_terminal(
                    order.vt_orderid,
                    has_reported_fill=int(getattr(order, "traded", 0) or 0) > 0,
                )
            if reported_fill_pending:
                terminal_reported = getattr(
                    self,
                    "_terminal_reported_traded",
                    None,
                )
                if terminal_reported is None:
                    terminal_reported = {}
                    self._terminal_reported_traded = terminal_reported
                terminal_reported[order.vt_orderid] = int(
                    getattr(order, "traded", 0) or 0
                )
            if not reported_fill_pending:
                self._pending_order_ids.discard(order.vt_orderid)
                self._pending_entries.pop(order.vt_orderid, None)

    def _on_trade(self, event: Event) -> None:
        trade: TradeData = event.data
        if (
            trade.vt_orderid not in self._pending_order_ids
            and trade.vt_orderid not in self._pending_entries
        ):
            return
        if trade.offset != Offset.OPEN:
            try:
                _trade_fill_id(trade)
                _trade_order_id(trade)
            except ValueError as exc:
                self._log(f"拒绝无法审计的平仓成交: {exc}")
                return

        # ── 开仓成交 → 记录持仓 ──
        if trade.offset == Offset.OPEN:
            direction = (
                SignalDirection.LONG
                if trade.direction == Direction.LONG
                else SignalDirection.SHORT
            )
            fill_volume = int(trade.volume)
            pending = self._pending_entries.get(trade.vt_orderid)
            if pending is None:
                self._log("忽略开仓成交: 缺少 TradeIntent")
                return
            opening_position = self._position is None
            if opening_position:
                self._position = pending.intent.position_from_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None) or datetime.now(),
                    entry_bar=self._bar_count,
                    active_symbol=getattr(trade, "symbol", None),
                    position_id=position_id_from_open_fill_id(
                        _runtime_open_fill_id(trade)
                    ),
                )
            else:
                if self._position.direction != direction:
                    self._log("忽略开仓成交: 成交方向与活动持仓不一致")
                    return
                self._position = self._position.merge_open_fill(
                    fill_price=float(trade.price),
                    fill_volume=fill_volume,
                    fill_time=getattr(trade, "datetime", None),
                )
            if pending.probe_intent_id and self._risk is not None:
                self._risk.record_probe_open_fill(
                    intent_id=pending.probe_intent_id,
                    position_id=self._position.position_id,
                    fill_volume=fill_volume,
                    order_id=trade.vt_orderid,
                )
            if opening_position:
                self._exit_manager.on_position_opened(self._position)
            else:
                self._exit_manager.on_position_updated(self._position)
            self._log(
                f"开仓成交: {direction.value} @ {trade.price} volume={fill_volume}"
            )
            pending = pending.apply_fill(fill_volume)
            reported_target = getattr(
                self,
                "_terminal_reported_traded",
                {},
            ).get(
                trade.vt_orderid,
                0,
            )
            if pending.complete or (
                reported_target > 0 and pending.filled_volume >= reported_target
            ):
                self._pending_order_ids.discard(trade.vt_orderid)
                self._pending_entries.pop(trade.vt_orderid, None)
                getattr(self, "_terminal_reported_traded", {}).pop(
                    trade.vt_orderid,
                    None,
                )
            else:
                self._pending_entries[trade.vt_orderid] = pending

        # ── 平仓成交 → 清理持仓 ──
        elif self._position is not None:
            fill_volume = int(trade.volume)
            closing_position = self._position
            if fill_volume < 1 or fill_volume > closing_position.volume:
                raise ValueError(
                    "invalid close fill volume: "
                    f"fill={fill_volume}, position={closing_position.volume}"
                )
            pnl = self._calc_pnl(float(trade.price), volume=fill_volume)
            remaining_quantity = max(0, closing_position.volume - fill_volume)
            fill_time = getattr(trade, "datetime", None) or datetime.now()
            risk_session = self._risk_session_resolver.resolve(
                fill_time,
                source="live_fill",
            )
            equity_value, equity_source, equity_scope = (
                self._account_equity_observation()
            )
            accepted = self._risk.record_close_fill(
                CloseFillRecord(
                    fill_id=_trade_fill_id(trade),
                    order_id=_trade_order_id(trade),
                    position_id=closing_position.position_id,
                    pnl_raw_points=pnl,
                    risk_session_key=risk_session.session_key,
                    observed_at=fill_time,
                    source="live_fill",
                    current_equity=equity_value,
                    equity_source=equity_source,
                    equity_scope=equity_scope,
                    max_drawdown_capability="disabled_by_design",
                ),
                remaining_quantity=remaining_quantity,
            )
            if not accepted:
                self._log(f"忽略重复平仓成交: {_trade_fill_id(trade)}")
                return
            self._log(
                f"平仓成交: @ {trade.price} "
                f"PnL={pnl:.0f} pts "
                f"累计已实现={self._risk._realized_points:.0f} pts"
            )
            self._position = closing_position.reduce_volume(fill_volume)
            if self._position is None:
                self._risk.finalize_position(
                    closing_position.position_id,
                    finalized_at=fill_time,
                    reason="live_close",
                )
                self._exit_manager.on_close()
                self._pending_order_ids.discard(trade.vt_orderid)
                pending_reversal = self._pending_reversal
                self._pending_reversal = None
                if pending_reversal is not None:
                    option_state = (
                        self._risk.get_state().get("option_d", {})
                        if self._risk is not None
                        else {}
                    )
                    if option_state.get("regime_state") == (
                        "PAUSED_UNTIL_NEXT_SESSION"
                    ):
                        self._log("反手开仓腿被 Option D 暂停态取消")
                    else:
                        self._approve_and_open_position(
                            pending_reversal.intent,
                            before_position=0,
                        )
            else:
                self._exit_manager.on_position_updated(self._position)

    # ════════════════════════════════════════
    # 交易操作
    # ════════════════════════════════════════

    def _open_position(
        self,
        intent: TradeIntent,
        *,
        probe_intent_id: str = "",
    ) -> None:
        """向 vnpy 发送开仓订单。"""
        signal = intent.signal
        direction = _vnpy_direction_from_target(signal.target_position)
        order_price = self._execution_price(signal.price, signal.target_position)
        order_req = OrderRequest(
            symbol=self._vt_symbol.split(".")[0] if "." in self._vt_symbol else self._vt_symbol,
            exchange=getattr(self, "_exchange", None) or Exchange.SHFE,
            direction=direction,
            type=OrderType.LIMIT,
            price=order_price,
            volume=abs(signal.target_position),
            offset=Offset.OPEN,
        )
        vt_orderid = self._send_order(order_req)
        if vt_orderid:
            self._pending_order_ids.add(vt_orderid)
            entries = getattr(self, "_pending_entries", None)
            if entries is None:
                self._pending_entries = {}
            self._pending_entries[vt_orderid] = PendingEntry(
                intent,
                probe_intent_id=probe_intent_id,
            )
            if probe_intent_id and self._risk is not None:
                self._risk.bind_probe_orders(probe_intent_id, [vt_orderid])
        elif probe_intent_id and self._risk is not None:
            self._risk.release_probe_reservation(probe_intent_id)
        self._last_accepted_grade = intent.grade
        self._last_event_id = intent.event_id
        self._log(
            f"发送开仓单: {direction.value} @ {order_price} "
            f"volume={abs(signal.target_position)} "
            f"grade={intent.grade} bsp={intent.bsp_type}"
        )

    def _approve_and_open_position(
        self,
        intent: TradeIntent,
        *,
        before_position: int,
    ) -> None:
        if self._risk is not None:
            transition = decompose_position_transition(
                before_position,
                intent.signal.target_position,
            )
            if transition.open_leg is None:
                return
            equity_value, _, _ = self._account_equity_observation()
            decision = self._risk.approve(
                intent.signal,
                current_equity=equity_value,
                before_position=before_position,
            )
            if not decision.approved:
                self._log(f"风控拒绝: {decision.reason}")
                return
        else:
            transition = decompose_position_transition(
                before_position,
                intent.signal.target_position,
            )
            if transition.open_leg is None:
                return
        open_intent = intent.for_open_leg(transition.open_leg.quantity)
        probe_intent_id = ""
        if self._risk is not None and self._risk.option_d_probe_available:
            option_state = self._risk.get_state()["option_d"]
            probe_intent_id = probe_intent_identity(
                option_state["probe_epoch_id"],
                open_intent.event_id,
            )
            self._risk.reserve_probe_intent(
                intent_id=probe_intent_id,
                decision_id=open_intent.decision.decision_id,
                event_id=open_intent.event_id,
                target_position=open_intent.signal.target_position,
                planned_volume=transition.open_leg.quantity,
                intent_snapshot=open_intent.to_runtime_snapshot(),
            )
        self._open_position(
            open_intent,
            probe_intent_id=probe_intent_id,
        )

    def _close_position(
        self,
        price: float,
        reason: str,
        description: str,
        *,
        volume: int | None = None,
    ) -> None:
        """向 vnpy 发送平仓订单。"""
        if self._position is None:
            return
        direction = Direction.SHORT if self._position.direction == SignalDirection.LONG else Direction.LONG
        quantity_delta = (
            -self._position.volume
            if self._position.direction == SignalDirection.LONG
            else self._position.volume
        )
        order_price = self._execution_price(price, quantity_delta)
        close_volume = self._position.volume if volume is None else int(volume)
        if close_volume < 1 or close_volume > self._position.volume:
            raise ValueError(
                "invalid close volume: "
                f"{close_volume} for position {self._position.volume}"
            )
        order_req = OrderRequest(
            symbol=self._vt_symbol.split(".")[0] if "." in self._vt_symbol else self._vt_symbol,
            exchange=getattr(self, "_exchange", None) or Exchange.SHFE,
            direction=direction,
            type=OrderType.LIMIT,
            price=order_price,
            volume=close_volume,
            offset=Offset.CLOSE,
        )
        vt_orderid = self._send_order(order_req)
        if vt_orderid:
            self._pending_order_ids.add(vt_orderid)
        self._log(
            f"发送平仓单: {direction.value} @ {order_price} ({reason}) {description}"
        )

    def _execution_price(self, price: float, quantity_delta: int) -> float:
        config = getattr(self, "_config", None)
        execution = config.execution if config is not None else None
        return adverse_fill_price(
            price,
            quantity_delta=quantity_delta,
            slippage_points=(execution.slippage_points if execution else 0.0),
            price_tick=(execution.price_tick if execution else 1.0),
        )

    def _send_order(self, request: OrderRequest) -> str:
        """Resolve the gateway and track one vn.py order id atomically."""
        contract = self.main_engine.get_contract(self._vt_symbol)
        gateway_name = getattr(contract, "gateway_name", "") if contract else ""
        if not gateway_name:
            self._log(f"拒绝发单: 未找到 {self._vt_symbol} 对应的交易接口")
            return ""
        return self.main_engine.send_order(request, gateway_name)

    # ════════════════════════════════════════
    # 出场规则检查
    # ════════════════════════════════════════

    def _check_exit(
        self,
        open: float,
        high: float,
        low: float,
        close: float,
        bar_end_time: datetime,
        intent: TradeIntent | None,
        atr: float | None,
    ) -> ExitSignal | None:
        """使用 ExitManager 检查出场条件。"""
        if self._position is None or self._exit_manager is None:
            return None

        chan = self._snapshot.current if self._snapshot else None
        if chan is None or self._decision_kernel is None:
            return None
        direction = (
            "long"
            if self._position.direction == SignalDirection.LONG
            else "short"
        )
        chan_snap = self._decision_kernel.build_exit_snapshot(
            chan,
            observed_at=bar_end_time,
            direction=direction,
            open_price=open,
            high_price=high,
            low_price=low,
            close_price=close,
            average_amplitude=atr,
            lv_idx=0,
        )

        return self._exit_manager.check(
            bar_end_time=bar_end_time,
            open=open,
            high=high,
            low=low,
            close=close,
            opposite_signal_triggered=_confirmed_opposite(
                intent,
                1 if direction == "long" else -1,
            ),
            chan_snapshot=chan_snap,
        )

    # ════════════════════════════════════════
    # 信号追踪
    # ════════════════════════════════════════

    def _record_signal(self, graded, timestamp: datetime) -> None:
        """Persist the event, assessment and accepted/rejected decision."""
        self._last_accepted_grade = graded.grade
        self._last_signal_key = getattr(graded, "signal_key", str(uuid.uuid4())[:12])
        if self._journal is not None:
            if graded.event is not None:
                self._journal.write_events([graded.event])
                if self._tracker is not None:
                    self._tracker.add(graded.event)
            if graded.assessment is not None:
                self._journal.write_assessments([graded.assessment])
            self._journal.write_decisions([graded.decision])
        self._log(
            f"信号: {graded.bsp_type} grade={graded.grade} "
            f"score={graded.structural_score:.2f} accepted={graded.accepted} "
            f"key={self._last_signal_key}"
        )

    def _update_pending_signals(self, timestamp: datetime) -> None:
        """检查待确认信号 — candidate → confirmed/invalidated/expired。"""
        if self._snapshot is None or self._extractor is None:
            return
        chan = self._snapshot.current
        if chan is None:
            return

        # 遍历当前 BSP 状态, 与 pending_signals 做比对
        for bsp in chan[0].bs_point_lst.bsp_iter():
            event = self._extractor.extract(bsp, chan=chan, bar_end_time=timestamp, lv_idx=0)
            if event is None or event.signal_key not in self._pending_signals:
                continue
            ctx = self._pending_signals[event.signal_key]
            ctx.last_seen = timestamp
            ctx.bars_held += 1

            if event.state.value == "confirmed" and ctx.bars_held > 0:
                self._log(f"信号确认: {ctx.signal_key} {ctx.primary_bsp} {ctx.grade}")
            elif event.state.value == "invalidated":
                self._log(f"信号失效: {ctx.signal_key}")
                del self._pending_signals[event.signal_key]

        # 清理超时未确认 (> 50 bars = ~12.5 hours for 15m)
        expired = [k for k, v in self._pending_signals.items() if v.bars_held > 50]
        for k in expired:
            self._log(f"信号过期: {k}")
            del self._pending_signals[k]

    # ════════════════════════════════════════
    # 工具
    # ════════════════════════════════════════

    def _calc_pnl(self, exit_price: float, *, volume: int | None = None) -> float:
        if self._position is None:
            return 0
        fee = self._config.execution.fee_points if self._config else 1.0
        return self._position.pnl_points(
            exit_price=exit_price,
            fee_points=fee,
            volume=volume,
        )

    def _account_equity(self) -> float | None:
        return self._account_equity_observation()[0]

    def _account_equity_observation(self) -> tuple[float | None, str, str]:
        fallback = self._config.sizing.capital if self._config else None
        try:
            oms = self.main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            balances = [float(account.balance) for account in accounts]
            if balances:
                value = sum(balances)
                if math.isfinite(value) and value > 0:
                    return value, "oms_balance", "oms_all_accounts"
                return None, "oms_balance", "oms_all_accounts"
            return fallback, "config_fallback", "oms_all_accounts"
        except Exception:
            return fallback, "config_fallback", "oms_all_accounts"

    def _advance_risk_session(self, timestamp: object, *, source: str):
        context = self._risk_session_resolver.resolve(
            timestamp,
            source=source,
        )
        if self._risk is not None:
            self._risk.advance_session(
                context.session_key,
                observed_at=context.observed_at,
                source=context.source,
            )
        return context

    def _account_available_funds(self) -> float | None:
        fallback = self._account_equity()
        try:
            oms = self.main_engine.get_engine("oms")
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

    def _log(self, msg: str) -> None:
        self.main_engine.write_log(f"[ChanLive] {msg}", self.engine_name)

    # ── 状态查询 (供 GUI 调用) ──

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def position_context(self) -> PositionContext | None:
        return self._position

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

    def engine_status(self) -> dict[str, Any]:
        """返回引擎状态的快照。"""
        decomposition = self.decomposition_state
        return {
            "bar_count": self._bar_count,
            "has_position": self.has_position,
            "decomposition": (
                {
                    "id": decomposition.decomposition_id,
                    "revision": decomposition.revision,
                    "regime": decomposition.regime.value,
                    "direction": decomposition.direction.value,
                    "lifecycle": decomposition.lifecycle.value,
                }
                if decomposition is not None
                else None
            ),
            "position": {
                "direction": self._position.direction.value if self._position else None,
                "entry_price": round(self._position.entry_price, 1) if self._position else None,
                "entry_grade": self._position.entry_grade if self._position else None,
                "entry_bar": self._position.entry_bar if self._position else None,
                "bars_held": (self._bar_count - self._position.entry_bar) if self._position else 0,
            },
            "risk": self._risk.get_state() if self._risk else {},
            "pending_signals": len(self._pending_signals),
            "vt_symbol": self._vt_symbol,
        }


def _confirmed_opposite(intent: TradeIntent | None, position: int) -> bool:
    if intent is None or intent.event is None or position == 0:
        return False
    if intent.event.state != SignalState.CONFIRMED:
        return False
    return (
        position > 0 and intent.event.direction == SignalDirection.SHORT
    ) or (position < 0 and intent.event.direction == SignalDirection.LONG)


def _vnpy_direction_from_target(target_position: int) -> Direction:
    """Map the signed target position to an opening order direction."""
    if target_position > 0:
        return Direction.LONG
    if target_position < 0:
        return Direction.SHORT
    raise ValueError("开仓目标仓位不能为 0")


def _trade_fill_id(trade: TradeData) -> str:
    value = str(
        getattr(trade, "vt_tradeid", "")
        or getattr(trade, "tradeid", "")
        or ""
    ).strip()
    if not value:
        raise ValueError("close fill is missing vt_tradeid/tradeid")
    return value


def _runtime_open_fill_id(trade: TradeData) -> str:
    value = str(
        getattr(trade, "vt_tradeid", "")
        or getattr(trade, "tradeid", "")
        or getattr(trade, "vt_orderid", "")
        or getattr(trade, "orderid", "")
        or ""
    ).strip()
    if not value:
        raise ValueError("open_fill_identity_missing")
    return value


def _trade_order_id(trade: TradeData) -> str:
    value = str(
        getattr(trade, "vt_orderid", "")
        or getattr(trade, "orderid", "")
        or ""
    ).strip()
    if not value:
        raise ValueError("close fill is missing vt_orderid/orderid")
    return value
