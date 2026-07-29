"""vnpy 实盘执行引擎 —— 对标 SimulatedExecutionEngine，但通过 vnpy 下单。

设计原则:
  - 与 chan_futures/execution.py 的 SimulatedExecutionEngine 接口完全一致
  - execute(signal) → Fill | None
  - 订单状态通过 vnpy EVENT_ORDER / EVENT_TRADE 异步回调
  - 对外暴露 pending 订单状态供查询

用法:
    engine = VnpyExecutionEngine(main_engine, fee_points=1.0, slippage_points=1.0)
    fill = engine.execute(signal)  # 同步发送订单，异步成交
    # 后续通过 engine._on_trade(...) 异步处理成交回调
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from chan_futures.execution import Fill, PositionState
from chan_futures.strategy import StrategySignal


class OrderStatus(StrEnum):
    PENDING = "pending"        # 已发送，等待成交
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


@dataclass
class PendingOrder:
    """一笔待成交订单的完整追踪。"""

    order_id: str
    signal: StrategySignal
    price: float
    volume: int
    status: OrderStatus = OrderStatus.PENDING
    filled_volume: int = 0
    avg_fill_price: float = 0.0
    sent_at: datetime = field(default_factory=datetime.now)
    last_updated: datetime = field(default_factory=datetime.now)
    retry_count: int = 0
    max_retries: int = 3
    timeout_seconds: int = 30  # 超时秒数


@dataclass
class VnpyExecutionEngine:
    """通过 vnpy MainEngine 发送订单的执行引擎。

    生命周期:
      1. 策略产出 signal → execute(signal) → 挂单
      2. vnpy 事件循环 → on_order_update / on_trade_update → 更新状态
      3. 超时或拒绝 → retry / cancel
    """

    def __init__(
        self,
        *,
        fee_points: float = 1.0,
        slippage_points: float = 1.0,
        max_retries: int = 3,
        order_timeout_seconds: int = 30,
    ) -> None:
        self.fee_points = float(fee_points)
        self.slippage_points = float(slippage_points)
        self.max_retries = max_retries
        self.order_timeout_seconds = order_timeout_seconds

        # ── 状态 ──
        self.state = PositionState()
        self.fills: list[Fill] = []
        self._pending: dict[str, PendingOrder] = {}   # order_id → PendingOrder
        self._main_engine: Any = None                  # vnpy MainEngine (由外部注入)
        self._vt_symbol: str = ""

    # ════════════════════════════════════════
    # 配置
    # ════════════════════════════════════════

    def configure(self, main_engine: Any, vt_symbol: str) -> None:
        """注入 vnpy 依赖 (避免构造函数强制依赖)。"""
        self._main_engine = main_engine
        self._vt_symbol = vt_symbol

    # ════════════════════════════════════════
    # 下单 (同步发送，异步成交)
    # ════════════════════════════════════════

    def execute(self, signal: StrategySignal) -> Fill | None:
        """同步发送订单到 vnpy，返回 None（实盘不立即产生 Fill）。

        Fill 将异步通过 on_trade_filled() 创建。
        """
        if self._main_engine is None:
            return None

        previous_position = self.state.position
        target_position = signal.target_position
        quantity_delta = target_position - previous_position

        if quantity_delta == 0:
            return None

        # ── 计算挂单价 (含滑点) ──
        order_price = self._fill_price(signal.price, quantity_delta)
        order_volume = abs(quantity_delta)

        # ── 方向 ──
        from vnpy.trader.constant import Direction, Offset, OrderType
        if quantity_delta > 0:
            direction = Direction.LONG
            offset = Offset.OPEN
        else:
            direction = Direction.SHORT
            offset = Offset.CLOSE

        # ── 构建 OrderRequest ──
        try:
            from vnpy.trader.object import OrderRequest
            req = OrderRequest(
                symbol=self._vt_symbol.split(".")[0] if "." in self._vt_symbol else self._vt_symbol,
                exchange=getattr(self, "_exchange", None) or "SHFE",
                direction=direction,
                type=OrderType.LIMIT,
                price=order_price,
                volume=order_volume,
                offset=offset,
            )
        except ImportError:
            # 无 vnpy 环境时静默失败
            return None

        # ── 发送 ──
        vt_orderids = self._main_engine.send_order(req)
        if not vt_orderids:
            return None

        order_id = str(vt_orderids[0])
        self._pending[order_id] = PendingOrder(
            order_id=order_id,
            signal=signal,
            price=order_price,
            volume=order_volume,
            max_retries=self.max_retries,
            timeout_seconds=self.order_timeout_seconds,
        )

        # ── 返回 None (Fill 异步产生) ──
        return None

    # ════════════════════════════════════════
    # 事件回调 (由 LiveTradingEngine 调用)
    # ════════════════════════════════════════

    def on_order_update(self, order: Any) -> None:
        """vnpy EVENT_ORDER → 更新挂单状态。"""
        if not hasattr(order, "vt_orderid"):
            return
        order_id = str(order.vt_orderid)
        if order_id not in self._pending:
            return

        po = self._pending[order_id]
        po.last_updated = datetime.now()

        from vnpy.trader.constant import Status
        status = getattr(order, "status", None)
        if status == Status.REJECTED:
            po.status = OrderStatus.REJECTED
            self._handle_rejection(po)
        elif status == Status.CANCELLED:
            po.status = OrderStatus.CANCELLED
        elif status == Status.ALLTRADED:
            po.status = OrderStatus.FILLED
            self._pending.pop(order_id, None)

    def on_trade_filled(self, trade: Any) -> None:
        """vnpy EVENT_TRADE → 记录成交。

        成交后更新 PositionState 并产生 Fill 记录。
        """
        if not hasattr(trade, "vt_orderid"):
            return
        order_id = str(trade.vt_orderid)

        fill_price = float(trade.price)
        fill_volume = float(trade.volume)

        # 如果 order 不在 _pending 中，可能是以前直接调用的
        po = self._pending.get(order_id)
        signal = po.signal if po else None

        # ── 更新 PositionState ──
        previous_position = self.state.position
        target_position = self.state.position  # 默认不变

        if signal is not None:
            target_position = signal.target_position

        quantity_delta = target_position - previous_position
        if quantity_delta == 0:
            return

        self.state.realized_points -= self.fee_points * abs(quantity_delta)
        self._apply_position_change(target_position, fill_price)

        # ── 产生 Fill 记录 ──
        fill = Fill(
            timestamp=datetime.now(),
            action=signal.action if signal else "unknown",
            previous_position=previous_position,
            target_position=target_position,
            quantity_delta=quantity_delta,
            signal_price=signal.price if signal else fill_price,
            fill_price=fill_price,
            realized_points=self.state.realized_points,
            equity_points=self.state.equity_points(fill_price),
            reason=signal.reason if signal else "manual",
            bsp_type=signal.bsp_type if signal else "",
            active_symbol=getattr(trade, "symbol", None),
        )
        self.fills.append(fill)

        if po:
            po.filled_volume += fill_volume
            po.avg_fill_price = (
                (po.avg_fill_price * (po.filled_volume - fill_volume) + fill_price * fill_volume)
                / po.filled_volume
            )
            if po.filled_volume >= po.volume:
                po.status = OrderStatus.FILLED
                self._pending.pop(order_id, None)

    # ════════════════════════════════════════
    # 超时 / 重试
    # ════════════════════════════════════════

    def check_timeouts(self) -> list[str]:
        """检查超时订单，返回需要取消的 order_id 列表。"""
        now = datetime.now()
        timed_out: list[str] = []
        for oid, po in self._pending.items():
            if po.status != OrderStatus.PENDING:
                continue
            elapsed = (now - po.sent_at).total_seconds()
            if elapsed > po.timeout_seconds:
                timed_out.append(oid)
                po.status = OrderStatus.TIMED_OUT
                self._handle_rejection(po)
        return timed_out

    def _handle_rejection(self, po: PendingOrder) -> None:
        """处理拒单或超时：尝试重试。"""
        if po.retry_count < po.max_retries:
            po.retry_count += 1
            po.status = OrderStatus.PENDING
            po.sent_at = datetime.now()
            # 由上层重新 send_order
        # 否则该 order 被丢弃，上层需要感知

    # ════════════════════════════════════════
    # PositionsState 操作 (复用原版逻辑)
    # ════════════════════════════════════════

    def _apply_position_change(self, target_position: int, fill_price: float) -> None:
        previous_position = self.state.position
        previous_avg = self.state.avg_price
        if previous_position != 0 and previous_avg is not None:
            if target_position == 0 or _sign(target_position) != _sign(previous_position):
                self.state.realized_points += previous_position * (fill_price - previous_avg)

        self.state.position = target_position
        if target_position == 0:
            self.state.avg_price = None
        elif previous_position == 0 or _sign(target_position) != _sign(previous_position):
            self.state.avg_price = fill_price

    def _fill_price(self, signal_price: float, quantity_delta: int) -> float:
        if quantity_delta > 0:
            return float(signal_price) + self.slippage_points
        return float(signal_price) - self.slippage_points

    def mark_to_market(self, price: float) -> float:
        return self.state.equity_points(float(price))

    # ── 状态查询 ──

    @property
    def has_pending_orders(self) -> bool:
        return len(self._pending) > 0

    @property
    def pending_orders(self) -> dict[str, PendingOrder]:
        return dict(self._pending)

    def order_status_summary(self) -> dict[str, int]:
        """各状态的挂单数量。"""
        counts: dict[str, int] = {}
        for po in self._pending.values():
            counts[po.status.value] = counts.get(po.status.value, 0) + 1
        return counts


def _sign(value: int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
