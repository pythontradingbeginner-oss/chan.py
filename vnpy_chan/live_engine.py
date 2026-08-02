"""实盘交易引擎 —— 从 vnpy bar 驱动完整策略管线。

与回测 _run_loop 共享同一套:
  - GradedChanStrategy (入场信号 + grade 过滤)
  - ExitManager (出场规则)
  - RiskManager (风控)
  - SignalExtractor (信号提取)

仅执行层不同: 回测用 SimulatedExecutionEngine, 实盘用 VnpyExecutionEngine。

状态机:
  idle → waiting_signal → pending_open → in_position → closing → idle

用法:
    engine = LiveTradingEngine(main_engine, event_engine)
    engine.init_strategy(config)
    engine.start()  # 订阅 EVENT_BAR
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
from signal_core import SignalExtractor, SignalDirection
from signal_core.lifecycle import SignalJournal, SignalLifecycleTracker
from strategy_policy.exit_rules import (
    ChanExitSnapshot,
    ExitManager,
    ExitSignal,
)
from chan_futures.config import StrategyConfig
from chan_futures.config_loader import load_config, make_exit_manager, make_graded_strategy
from chan_futures.strategy import StrategySignal
from chan_futures.graded_strategy import GradedChanStrategy
from chan_futures.risk import RiskConfig, RiskManager

from .converter import bar_to_klu, window_to_kl_type
from .snapshot import ChanSnapshotManager


# ════════════════════════════════════════════════════════════════
# 引擎状态
# ════════════════════════════════════════════════════════════════

@dataclass
class _PositionContext:
    """活动持仓的快照 (入场信息 —— 驱动出场逻辑)。"""

    direction: SignalDirection
    entry_price: float
    entry_time: datetime
    entry_bar: int
    entry_grade: str = "standard"
    bi_begin_price: float | None = None
    zs_high: float | None = None
    zs_low: float | None = None
    invalidation_price: float | None = None
    initial_stop_price: float | None = None
    signal_key: str = ""
    volume: int = 1


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
    """有状态的实盘缠论交易引擎。

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
        self._exit_manager: ExitManager | None = None
        self._risk: RiskManager | None = None
        self._journal: SignalJournal | None = None
        self._tracker: SignalLifecycleTracker | None = None

        # ── 状态 ──
        self._position: _PositionContext | None = None
        self._pending_signals: dict[str, _SignalContext] = {}
        self._bar_count: int = 0
        self._vt_symbol: str = ""

        # ── 订单追踪 ──
        self._pending_order_ids: set[str] = set()
        self._pending_entry_contexts: dict[str, dict[str, Any]] = {}
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
        kl_type = window_to_kl_type(15)  # FIXME: read from config.kl_type

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

        # ── 共享决策管线 ──
        self._wrapper = make_graded_strategy(cfg)

        # ── SignalExtractor ──
        self._extractor = SignalExtractor(symbol="RB", timeframe="15m")

        # ── ExitManager ──
        self._exit_manager = make_exit_manager(cfg)

        # ── RiskManager ──
        self._risk = RiskManager(RiskConfig(
            max_abs_position=cfg.risk.max_abs_position,
            max_loss_points=cfg.risk.max_loss_points,
            daily_loss_limit=cfg.risk.daily_loss_limit,
            max_consecutive_losses=cfg.risk.max_consecutive_losses,
            max_drawdown_pct=None,  # 实盘中由 vnpy 账户权益驱动
        ))

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
        if self._config is None or self._chan is None or self._snapshot is None:
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

        # ── 1. 检查出场规则 (如果当前有持仓) ──
        if self._position is not None and self._exit_manager is not None:
            exit_signal = self._check_exit(
                open=float(bar.open_price),
                high=float(bar.high_price),
                low=float(bar.low_price),
                close=price,
                bar_end_time=timestamp,
            )
            if exit_signal is not None:
                self._close_position(exit_signal.exit_price, exit_signal.reason_code, exit_signal.description)
                return

        # ── 2. 检查入场信号 ──
        if self._position is None:
            chan_snap = self._snapshot.current
            if chan_snap is None:
                return

            graded = self._wrapper.evaluate_bar(
                chan=chan_snap,
                current_position=0,
                price=price,
                timestamp=timestamp,
                active_symbol=bar.symbol,
                lv_idx=0,
                extractor=self._extractor,
                account_equity=self._account_equity(),
                atr=atr,
            )
            if graded is not None:
                self._record_signal(graded, timestamp)
                if not graded.accepted:
                    self._log(
                        "决策拒绝: " + ",".join(graded.decision.reason_codes)
                    )
                    return

                sig = graded.signal

                # 风控审批
                if self._risk is not None:
                    decision = self._risk.approve(sig)
                    if not decision.approved:
                        self._log(f"风控拒绝: {decision.reason}")
                        return

                # 发送开仓订单
                self._open_position(
                    sig,
                    graded.bsp_type,
                    graded.grade,
                    graded.event_id,
                    event=graded.event,
                    decision=graded.decision,
                    signal_key=graded.signal_key,
                )

        # ── 3. 信号状态更新 (pending signals → confirmed/invalidated) ──
        self._update_pending_signals(timestamp)

    def _on_order(self, event: Event) -> None:
        order: OrderData = event.data
        if order.vt_orderid not in self._pending_order_ids:
            return
        self._log(f"订单状态: {order.vt_orderid} → {order.status.value}")
        if order.status in {Status.CANCELLED, Status.REJECTED}:
            self._pending_order_ids.discard(order.vt_orderid)
            self._pending_entry_contexts.pop(order.vt_orderid, None)

    def _on_trade(self, event: Event) -> None:
        trade: TradeData = event.data
        if (
            trade.vt_orderid not in self._pending_order_ids
            and trade.vt_orderid not in self._pending_entry_contexts
        ):
            return

        # ── 开仓成交 → 记录持仓 ──
        if trade.offset == Offset.OPEN:
            direction = (
                SignalDirection.LONG
                if trade.direction == Direction.LONG
                else SignalDirection.SHORT
            )
            fill_volume = int(trade.volume)
            pending = self._pending_entry_contexts.get(trade.vt_orderid, {})
            event_snapshot = pending.get("event")
            decision_snapshot = pending.get("decision")
            if self._position is not None and self._position.direction == direction:
                previous_volume = self._position.volume
                total_volume = previous_volume + fill_volume
                entry_price = (
                    self._position.entry_price * previous_volume
                    + float(trade.price) * fill_volume
                ) / total_volume
            else:
                total_volume = fill_volume
                entry_price = float(trade.price)
            self._position = _PositionContext(
                direction=direction,
                entry_price=entry_price,
                entry_time=datetime.now(),
                entry_bar=self._bar_count,
                entry_grade=pending.get(
                    "grade", getattr(self, "_last_accepted_grade", "standard")
                ),
                bi_begin_price=(
                    event_snapshot.bi_begin_price if event_snapshot else None
                ),
                zs_high=event_snapshot.zs_high if event_snapshot else None,
                zs_low=event_snapshot.zs_low if event_snapshot else None,
                invalidation_price=(
                    decision_snapshot.setup_invalidation_price
                    if decision_snapshot else None
                ),
                initial_stop_price=(
                    decision_snapshot.execution_stop_price
                    if decision_snapshot else None
                ),
                signal_key=pending.get(
                    "signal_key", getattr(self, "_last_signal_key", "")
                ),
                volume=total_volume,
            )
            self._exit_manager.on_entry(
                direction=direction,
                entry_price=entry_price,
                bi_begin_price=self._position.bi_begin_price,
                zs_high=self._position.zs_high,
                zs_low=self._position.zs_low,
                initial_stop_price=self._position.initial_stop_price,
                invalidation_price=self._position.invalidation_price,
                entry_grade=self._position.entry_grade,
            )
            self._log(
                f"开仓成交: {direction.value} @ {trade.price} volume={fill_volume}"
            )
            pending["filled_volume"] = int(pending.get("filled_volume", 0)) + fill_volume
            if pending["filled_volume"] >= int(pending.get("planned_volume", fill_volume)):
                self._pending_order_ids.discard(trade.vt_orderid)
                self._pending_entry_contexts.pop(trade.vt_orderid, None)

        # ── 平仓成交 → 清理持仓 ──
        elif self._position is not None:
            fill_volume = int(trade.volume)
            pnl = self._calc_pnl(float(trade.price), volume=fill_volume)
            self._risk.on_fill(pnl_points=pnl, fill_time=datetime.now())
            self._log(
                f"平仓成交: @ {trade.price} "
                f"PnL={pnl:.0f} pts "
                f"累计已实现={self._risk._realized_points:.0f} pts"
            )
            self._position.volume = max(0, self._position.volume - fill_volume)
            if self._position.volume == 0:
                self._position = None
                self._exit_manager.on_close()
                self._pending_order_ids.discard(trade.vt_orderid)

    # ════════════════════════════════════════
    # 交易操作
    # ════════════════════════════════════════

    def _open_position(
        self,
        signal: StrategySignal,
        bsp_type: str,
        grade: str,
        event_id: str,
        *,
        event: Any | None = None,
        decision: Any | None = None,
        signal_key: str = "",
    ) -> None:
        """向 vnpy 发送开仓订单。"""
        direction = _vnpy_direction_from_target(signal.target_position)
        order_req = OrderRequest(
            symbol=self._vt_symbol.split(".")[0] if "." in self._vt_symbol else self._vt_symbol,
            exchange=getattr(self, "_exchange", None) or Exchange.SHFE,
            direction=direction,
            type=OrderType.LIMIT,
            price=signal.price,
            volume=abs(signal.target_position),
            offset=Offset.OPEN,
        )
        vt_orderid = self._send_order(order_req)
        if vt_orderid:
            self._pending_order_ids.add(vt_orderid)
            contexts = getattr(self, "_pending_entry_contexts", None)
            if contexts is None:
                self._pending_entry_contexts = {}
            self._pending_entry_contexts[vt_orderid] = {
                "event": event,
                "decision": decision,
                "grade": grade,
                "signal_key": signal_key,
                "planned_volume": abs(signal.target_position),
                "filled_volume": 0,
            }
        self._last_accepted_grade = grade
        self._last_event_id = event_id
        self._log(
            f"发送开仓单: {direction.value} @ {signal.price} "
            f"volume={abs(signal.target_position)} grade={grade} bsp={bsp_type}"
        )

    def _close_position(self, price: float, reason: str, description: str) -> None:
        """向 vnpy 发送平仓订单。"""
        if self._position is None:
            return
        direction = Direction.SHORT if self._position.direction == SignalDirection.LONG else Direction.LONG
        order_req = OrderRequest(
            symbol=self._vt_symbol.split(".")[0] if "." in self._vt_symbol else self._vt_symbol,
            exchange=getattr(self, "_exchange", None) or Exchange.SHFE,
            direction=direction,
            type=OrderType.LIMIT,
            price=price,
            volume=self._position.volume,
            offset=Offset.CLOSE,
        )
        vt_orderid = self._send_order(order_req)
        if vt_orderid:
            self._pending_order_ids.add(vt_orderid)
        self._log(f"发送平仓单: {direction.value} @ {price} ({reason}) {description}")

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
    ) -> ExitSignal | None:
        """使用 ExitManager 检查出场条件。"""
        if self._position is None or self._exit_manager is None:
            return None

        chan_snap = self._build_chan_exit_snapshot()

        return self._exit_manager.check(
            bar_end_time=bar_end_time,
            open=open,
            high=high,
            low=low,
            close=close,
            opposite_signal_triggered=False,
            chan_snapshot=chan_snap,
        )

    def _build_chan_exit_snapshot(self) -> ChanExitSnapshot | None:
        """从当前 CChan 快照构建缠论结构快照。"""
        chan = self._snapshot.current if self._snapshot else None
        if chan is None:
            return None

        try:
            kl_list = chan[0]
            seg_list = kl_list.seg_list
            last_seg = seg_list.lst[-1] if seg_list.lst else None
            return ChanExitSnapshot(
                available_at=datetime.now(),
                segment_complete=last_seg is not None and last_seg.is_sure if last_seg else False,
                segment_direction="up" if last_seg and last_seg.dir.value > 0 else "down" if last_seg else None,
            )
        except Exception:
            return None

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
        pnl = exit_price - self._position.entry_price
        if self._position.direction == SignalDirection.SHORT:
            pnl = -pnl
        fee = self._config.execution.fee_points if self._config else 1.0
        lots = self._position.volume if volume is None else volume
        return (pnl - fee * 2) * lots

    def _account_equity(self) -> float | None:
        fallback = self._config.sizing.capital if self._config else None
        try:
            oms = self.main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            balances = [float(account.balance) for account in accounts]
            return sum(balances) if balances else fallback
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
    def position_context(self) -> _PositionContext | None:
        return self._position

    def engine_status(self) -> dict[str, Any]:
        """返回引擎状态的快照。"""
        return {
            "bar_count": self._bar_count,
            "has_position": self.has_position,
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


def _vnpy_direction_from_target(target_position: int) -> Direction:
    """Map the signed target position to an opening order direction."""
    if target_position > 0:
        return Direction.LONG
    if target_position < 0:
        return Direction.SHORT
    raise ValueError("开仓目标仓位不能为 0")
