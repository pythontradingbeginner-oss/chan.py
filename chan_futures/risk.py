"""风险管理模块 —— 有状态的风控检查。

风控维度：
  - max_abs_position: 最大持仓手数（始终生效）
  - max_loss_points: 累计最大亏损（全局，累计已实现亏损超限即停止）
  - daily_loss_limit: 日内亏损限额（交易日累计已实现亏损超限则当日不再开仓）
  - max_consecutive_losses: 连续亏损笔数上限（达到后暂停，直到出现盈利）
  - max_drawdown_pct: 从权益峰值回撤超过阈值暂停开仓

RiskManager 已从无状态（仅 approve 检查）扩展为有状态对象：
  - 通过 on_fill() 更新内部追踪状态
  - 通过 approve() 做实时风控决策
  - 通过 get_state() 和 summary() 供审计
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .strategy import StrategySignal
from .position_transition import decompose_position_transition
from .option_d import OptionDController
from .risk_policy import RiskGateMode, RiskProfile, resolve_gate_modes


RISK_ACCOUNTING_STATE_VERSION = "open005-risk-runtime-v1"
OPTION_D_STATE_VERSION = "open005-risk-runtime-v2"


# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════


@dataclass(frozen=True)
class RiskConfig:
    max_abs_position: int = 1
    max_loss_points: float | None = None           # 累计已实现亏损上限
    daily_loss_limit: float | None = None           # 日累计亏损上限
    max_consecutive_losses: int | None = None       # 连续亏损笔数上限
    max_drawdown_pct: float | None = None           # 峰值权益回撤百分比
    require_session_key: bool = False               # RB 正式运行路径失败关闭
    profile: RiskProfile = RiskProfile.CURRENT_ENFORCED
    max_loss_points_mode: RiskGateMode | None = None
    daily_loss_limit_mode: RiskGateMode | None = None
    max_consecutive_losses_mode: RiskGateMode | None = None
    max_drawdown_pct_mode: RiskGateMode | None = None

    def gate_modes(self) -> dict[str, RiskGateMode]:
        return resolve_gate_modes(
            self.profile,
            {
                "max_loss_points": self.max_loss_points_mode,
                "daily_loss_limit": self.daily_loss_limit_mode,
                "max_consecutive_losses": self.max_consecutive_losses_mode,
                "max_drawdown_pct": self.max_drawdown_pct_mode,
            },
        )


# ═══════════════════════════════════════════
# RiskDecision
# ═══════════════════════════════════════════


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = "approved"
    hard_failures: tuple[str, ...] = ()
    enforced_breaches: tuple[str, ...] = ()
    observed_breaches: tuple[str, ...] = ()
    evaluated_gates: tuple[str, ...] = ()
    option_d_regime_state: str = ""
    option_d_probe_epoch_id: str = ""


@dataclass(frozen=True)
class CloseFillRecord:
    """One accepted close fill at the risk-accounting boundary."""

    fill_id: str
    order_id: str
    position_id: str
    pnl_raw_points: float
    risk_session_key: object | None
    observed_at: object | None
    source: str
    current_equity: float | None = None
    equity_source: str = ""
    equity_scope: str = ""
    max_drawdown_capability: str = ""


@dataclass
class PositionPnlAggregator:
    """PnL accumulated from accepted close fills for one position."""

    position_id: str
    raw_close_pnl_points: float = 0.0
    normalized_close_pnl_points: float = 0.0
    close_fill_ids: list[str] = field(default_factory=list)
    remaining_quantity: int = 0

    def to_state(self) -> dict:
        return {
            "position_id": self.position_id,
            "raw_close_pnl_points": self.raw_close_pnl_points,
            "normalized_close_pnl_points": self.normalized_close_pnl_points,
            "close_fill_ids": list(self.close_fill_ids),
            "remaining_quantity": self.remaining_quantity,
        }

    @classmethod
    def from_state(cls, state: dict) -> "PositionPnlAggregator":
        return cls(
            position_id=str(state["position_id"]),
            raw_close_pnl_points=float(
                state.get("raw_close_pnl_points", 0.0) or 0.0
            ),
            normalized_close_pnl_points=float(
                state.get("normalized_close_pnl_points", 0.0) or 0.0
            ),
            close_fill_ids=[str(value) for value in state.get("close_fill_ids", [])],
            remaining_quantity=int(state.get("remaining_quantity", 0) or 0),
        )


@dataclass(frozen=True)
class PositionFinalizeResult:
    position_id: str
    pnl_raw_points: float
    pnl_normalized_points: float
    result: str
    consecutive_losses: int


# ═══════════════════════════════════════════
# RiskManager
# ═══════════════════════════════════════════


class RiskManager:
    """有状态的风控管理器。

    用法:
        risk = RiskManager(RiskConfig(daily_loss_limit=200))
        for each fill:
            risk.on_fill(fill)   # 更新累计亏损和连续亏损计数
        decision = risk.approve(signal, current_equity)
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        initial_equity: float | None = None,
        initial_equity_source: str = "",
        equity_scope: str = "",
        max_drawdown_capability: str = "",
        next_session_resolver: Callable[[object], object] | None = None,
    ) -> None:
        self.config = config or RiskConfig()

        # ── 累计状态 ──
        self._realized_points: float = 0.0
        self._peak_equity: float = (
            float(initial_equity) if _valid_equity(initial_equity) else 0.0
        )
        self._equity_source: str | None = initial_equity_source or None
        self._equity_scope: str | None = equity_scope or None
        self._equity_unit: str = "account_currency"
        self._max_drawdown_capability: str | None = (
            max_drawdown_capability or None
        )
        self._has_authoritative_oms_equity = initial_equity_source == "oms_balance"
        self._total_fills: int = 0

        # ── 日内状态 ──
        self._daily_realized: float = 0.0
        self._current_session_key: str | None = None
        self._last_session_observed_at: pd.Timestamp | None = None
        self._session_source: str | None = None

        # ── 连续亏损状态 ──
        self._consecutive_losses: int = 0
        self._last_fill_pnl: float | None = None

        # ── OPEN005 幂等成交与仓位级结算状态 ──
        self._processed_close_fill_ids: set[str] = set()
        self._active_position_aggregators: dict[str, PositionPnlAggregator] = {}
        self._finalized_position_ids: set[str] = set()
        self._finalized_position_results: dict[str, dict] = {}
        self._legacy_fill_sequence: int = 0
        if self.config.profile == RiskProfile.D_EXPERIMENT and next_session_resolver is None:
            from data_foundation import RBTradingCalendar

            next_session_resolver = RBTradingCalendar.load_default().next_trading_day
        self._option_d = OptionDController(
            enabled=self.config.profile == RiskProfile.D_EXPERIMENT,
            loss_threshold=self.config.max_consecutive_losses,
            next_session=next_session_resolver,
        )

    # ── 核心接口 ──

    def approve(
        self,
        signal: StrategySignal,
        *,
        current_equity: float | None = None,
        before_position: int = 0,
    ) -> RiskDecision:
        """在开仓/加仓前做风控检查。

        Args:
            signal: 策略信号
            current_equity: 当前账户权益，与峰值权益保持相同货币单位
        """
        self.observe_equity(current_equity)
        try:
            transition = decompose_position_transition(
                before_position,
                signal.target_position,
            )
        except ValueError as exc:
            failure = f"position_transition_invalid: {exc}"
            return RiskDecision(False, failure, hard_failures=(failure,))

        if not transition.increases_exposure:
            return RiskDecision(True, "risk_reducing")

        modes = self.config.gate_modes()
        hard_failures: list[str] = []
        if (
            self.config.daily_loss_limit is not None
            and modes["daily_loss_limit"] != RiskGateMode.DISABLED
            and self.config.require_session_key
            and self._current_session_key is None
        ):
            hard_failures.append("risk_session_uninitialized")
        if (
            self.config.profile == RiskProfile.D_EXPERIMENT
            and self._current_session_key is None
            and "risk_session_uninitialized" not in hard_failures
        ):
            hard_failures.append("risk_session_uninitialized")
        if (
            self.config.max_drawdown_pct is not None
            and modes["max_drawdown_pct"] != RiskGateMode.DISABLED
            and not _valid_equity(current_equity)
        ):
            hard_failures.append("account_equity_unavailable")
        if abs(transition.open_leg.target_position) > self.config.max_abs_position:
            hard_failures.append("max_abs_position")

        evaluators = {
            "max_loss_points": self._max_loss_breach,
            "daily_loss_limit": self._daily_loss_breach,
            "max_consecutive_losses": self._consecutive_loss_breach,
            "max_drawdown_pct": lambda: self._drawdown_breach(current_equity),
        }
        evaluated: list[str] = []
        enforced: list[str] = []
        observed: list[str] = []
        for gate, evaluator in evaluators.items():
            mode = modes[gate]
            if mode == RiskGateMode.DISABLED or not self._gate_is_configured(gate):
                continue
            evaluated.append(gate)
            breach = evaluator()
            if breach is None:
                continue
            if mode == RiskGateMode.ENFORCE:
                enforced.append(breach)
            else:
                observed.append(breach)

        option_d_breach = self._option_d.entry_block_reason()
        if option_d_breach is not None:
            enforced.append(option_d_breach)

        reason = (
            hard_failures[0]
            if hard_failures
            else enforced[0]
            if enforced
            else "approved"
        )
        return RiskDecision(
            approved=not hard_failures and not enforced,
            reason=reason,
            hard_failures=tuple(hard_failures),
            enforced_breaches=tuple(enforced),
            observed_breaches=tuple(observed),
            evaluated_gates=tuple(evaluated),
            option_d_regime_state=self._option_d.state.regime_state.value,
            option_d_probe_epoch_id=self._option_d.state.probe_epoch_id or "",
        )

    def _gate_is_configured(self, gate: str) -> bool:
        return getattr(self.config, gate) is not None

    def _max_loss_breach(self) -> str | None:
        limit = self.config.max_loss_points
        if limit is not None and self._realized_points <= -abs(limit):
            return (
                f"max_loss_points: realized={self._realized_points:.0f} <= "
                f"-{abs(limit):.0f}"
            )
        return None

    def _daily_loss_breach(self) -> str | None:
        limit = self.config.daily_loss_limit
        if limit is not None and self._daily_realized <= -abs(limit):
            return f"daily_loss_limit: daily_realized={self._daily_realized:.0f}"
        return None

    def _consecutive_loss_breach(self) -> str | None:
        limit = self.config.max_consecutive_losses
        if limit is not None and self._consecutive_losses >= limit:
            return f"max_consecutive_losses: {self._consecutive_losses} >= {limit}"
        return None

    def _drawdown_breach(self, current_equity: float | None) -> str | None:
        limit = self.config.max_drawdown_pct
        if limit is None or not _valid_equity(current_equity) or self._peak_equity <= 0:
            return None
        drawdown = (self._peak_equity - float(current_equity)) / self._peak_equity
        if drawdown >= limit:
            return f"max_drawdown: {drawdown:.2%} >= {limit:.2%}"
        return None

    def on_fill(
        self,
        *,
        pnl_points: float,
        fill_time: object | None = None,
        current_equity: float | None = None,
        session_key: object | None = None,
        session_source: str = "fill",
        equity_source: str = "",
        equity_scope: str = "",
        max_drawdown_capability: str = "",
    ) -> None:
        """兼容旧一手调用：一次调用代表一个完整仓位已平仓。

        Args:
            pnl_points: 平仓实现的盈亏（点数）。开仓时传 0。
            fill_time: 成交时间（用于 session 顺序审计和兼容路径）
            current_equity: 当前账户权益，与峰值权益保持相同货币单位
            session_key: 交易日键；RB 正式路径必须显式传入
        """
        self._legacy_fill_sequence += 1
        suffix = str(self._legacy_fill_sequence)
        position_id = f"legacy-position-{suffix}"
        self.record_close_fill(
            CloseFillRecord(
                fill_id=f"legacy-fill-{suffix}",
                order_id=f"legacy-order-{suffix}",
                position_id=position_id,
                pnl_raw_points=pnl_points,
                risk_session_key=session_key,
                observed_at=fill_time,
                source=session_source,
                current_equity=current_equity,
                equity_source=equity_source,
                equity_scope=equity_scope,
                max_drawdown_capability=max_drawdown_capability,
            ),
            remaining_quantity=0,
        )
        self.finalize_position(
            position_id,
            finalized_at=fill_time,
            reason="legacy_on_fill",
        )

    def record_close_fill(
        self,
        fill: CloseFillRecord,
        *,
        remaining_quantity: int,
    ) -> bool:
        """Record one close fill without changing the position loss streak.

        Returns ``False`` for a duplicate ``fill_id`` and leaves all state
        untouched. The caller must invoke :meth:`finalize_position` only after
        the accepted fill makes the logical position quantity exactly zero.
        """
        fill_id = _required_identity(fill.fill_id, "fill_id")
        if fill_id in self._processed_close_fill_ids:
            return False
        order_id = _required_identity(fill.order_id, "order_id")
        position_id = _required_identity(fill.position_id, "position_id")
        del order_id  # Required for the event contract; not an aggregation key.
        if position_id in self._finalized_position_ids:
            raise ValueError(f"position_already_finalized: {position_id}")
        if int(remaining_quantity) < 0:
            raise ValueError("remaining_quantity must be >= 0")

        pnl_raw = _finite_pnl(fill.pnl_raw_points)
        pnl_normalized = round(pnl_raw, 1)

        if fill.risk_session_key is not None:
            self.advance_session(
                fill.risk_session_key,
                observed_at=fill.observed_at,
                source=fill.source,
            )
        elif self.config.daily_loss_limit is not None and self.config.require_session_key:
            raise ValueError("risk_session_key_required")
        elif fill.observed_at is not None and self.config.daily_loss_limit is not None:
            self.advance_session(
                _resolve_date(fill.observed_at),
                observed_at=fill.observed_at,
                source="legacy_fill_date",
            )

        aggregator = self._active_position_aggregators.get(position_id)
        if aggregator is None:
            aggregator = PositionPnlAggregator(position_id=position_id)
            self._active_position_aggregators[position_id] = aggregator

        self._processed_close_fill_ids.add(fill_id)
        self._total_fills += 1
        self._realized_points += pnl_normalized
        if self._current_session_key is not None:
            self._daily_realized += pnl_normalized
        aggregator.raw_close_pnl_points += pnl_raw
        aggregator.normalized_close_pnl_points += pnl_normalized
        aggregator.close_fill_ids.append(fill_id)
        aggregator.remaining_quantity = int(remaining_quantity)

        self.observe_equity(
            fill.current_equity,
            source=fill.equity_source,
            scope=fill.equity_scope,
            max_drawdown_capability=fill.max_drawdown_capability,
        )
        return True

    def finalize_position(
        self,
        position_id: str,
        *,
        finalized_at: object | None = None,
        reason: str = "",
    ) -> PositionFinalizeResult:
        """Finalize one flat position and update the true loss streak once."""
        normalized_id = _required_identity(position_id, "position_id")
        if normalized_id in self._finalized_position_ids:
            raise ValueError(f"position_already_finalized: {normalized_id}")
        aggregator = self._active_position_aggregators.get(normalized_id)
        if aggregator is None:
            raise ValueError(f"position_aggregator_missing: {normalized_id}")
        if aggregator.remaining_quantity != 0:
            raise ValueError(
                "position_not_flat: "
                f"position_id={normalized_id}, "
                f"remaining_quantity={aggregator.remaining_quantity}"
            )

        normalized_pnl = round(aggregator.raw_close_pnl_points, 1)
        if normalized_pnl < 0:
            result = "loss"
            self._consecutive_losses += 1
        elif normalized_pnl > 0:
            result = "win"
            self._consecutive_losses = 0
        else:
            result = "flat"
        self._last_fill_pnl = normalized_pnl

        self._active_position_aggregators.pop(normalized_id)
        self._finalized_position_ids.add(normalized_id)
        result_record = {
            "position_id": normalized_id,
            "raw_close_pnl_points": aggregator.raw_close_pnl_points,
            "normalized_position_pnl_points": normalized_pnl,
            "result": result,
            "close_fill_ids": list(aggregator.close_fill_ids),
            "finalized_at": (
                _normalize_observed_at(finalized_at).isoformat()
                if finalized_at is not None
                else None
            ),
            "reason": str(reason),
        }
        self._finalized_position_results[normalized_id] = result_record
        self._option_d.on_position_finalized(
            position_id=normalized_id,
            result=result,
            session_key=self._current_session_key,
            consecutive_losses=self._consecutive_losses,
        )
        return PositionFinalizeResult(
            position_id=normalized_id,
            pnl_raw_points=aggregator.raw_close_pnl_points,
            pnl_normalized_points=normalized_pnl,
            result=result,
            consecutive_losses=self._consecutive_losses,
        )

    def advance_session(
        self,
        session_key: object,
        *,
        observed_at: object | None = None,
        source: str = "",
    ) -> bool:
        """Advance the risk clock without relying on a fill side effect."""
        normalized = _normalize_session_key(session_key)
        observed = _normalize_observed_at(observed_at)

        if self._current_session_key is None:
            self._current_session_key = normalized
            self._last_session_observed_at = observed
            self._session_source = str(source) or None
            self._option_d.advance_session(normalized)
            return False

        current = pd.Timestamp(self._current_session_key)
        candidate = pd.Timestamp(normalized)
        if candidate < current:
            raise ValueError(
                "risk_session_regression: "
                f"current={self._current_session_key}, candidate={normalized}"
            )

        if candidate == current:
            if observed is not None and (
                self._last_session_observed_at is None
                or observed > self._last_session_observed_at
            ):
                self._last_session_observed_at = observed
                self._session_source = str(source) or self._session_source
            return False

        self._current_session_key = normalized
        self._last_session_observed_at = observed
        self._session_source = str(source) or None
        self._daily_realized = 0.0
        self._option_d.advance_session(normalized)
        return True

    @property
    def option_d_probe_available(self) -> bool:
        return self._option_d.probe_available

    def reserve_probe_intent(self, **kwargs) -> None:
        self._option_d.reserve_probe_intent(**kwargs)

    def bind_probe_orders(self, intent_id: str, order_ids: list[str]) -> None:
        self._option_d.bind_probe_orders(intent_id, order_ids)

    def record_probe_open_fill(self, **kwargs) -> None:
        self._option_d.record_probe_open_fill(**kwargs)

    def mark_probe_order_terminal(
        self,
        order_id: str,
        *,
        has_reported_fill: bool = False,
    ) -> bool:
        return self._option_d.mark_probe_order_terminal(
            order_id,
            has_reported_fill=has_reported_fill,
        )

    def release_probe_reservation(self, intent_id: str) -> None:
        self._option_d.release_probe_reservation(intent_id)

    def current_reserved_probe_intent(self) -> dict | None:
        return self._option_d.current_reserved_intent()

    def observe_equity(
        self,
        current_equity: float | None,
        *,
        source: str = "",
        scope: str = "",
        max_drawdown_capability: str = "",
    ) -> None:
        """Observe mark-to-market account equity and update its running peak."""
        if source:
            first_authoritative_oms = (
                source == "oms_balance"
                and not self._has_authoritative_oms_equity
            )
            self._equity_source = source
            if source == "oms_balance" and _valid_equity(current_equity):
                self._has_authoritative_oms_equity = True
        else:
            first_authoritative_oms = False
        if scope:
            self._equity_scope = scope
        if max_drawdown_capability:
            self._max_drawdown_capability = max_drawdown_capability
        if not _valid_equity(current_equity):
            return
        if first_authoritative_oms:
            self._peak_equity = float(current_equity)
        elif float(current_equity) > self._peak_equity:
            self._peak_equity = float(current_equity)

    # ── 状态查询 ──

    def get_state(self) -> dict:
        """返回当前风控状态（供序列化/审计）。"""
        state = {
            "schema_version": (
                OPTION_D_STATE_VERSION
                if self.config.profile == RiskProfile.D_EXPERIMENT
                else RISK_ACCOUNTING_STATE_VERSION
            ),
            "realized_points": self._realized_points,
            "peak_equity": self._peak_equity,
            "total_fills": self._total_fills,
            "daily_realized": self._daily_realized,
            "current_session_key": self._current_session_key,
            "current_risk_session": self._current_session_key,
            # Compatibility alias retained for persisted v1 CTA state.
            "current_date": self._current_session_key,
            "last_session_observed_at": (
                self._last_session_observed_at.isoformat()
                if self._last_session_observed_at is not None
                else None
            ),
            "session_source": self._session_source,
            "consecutive_losses": self._consecutive_losses,
            "true_consecutive_losses": self._consecutive_losses,
            "processed_close_fill_ids": sorted(self._processed_close_fill_ids),
            "active_position_aggregators": {
                position_id: aggregator.to_state()
                for position_id, aggregator in sorted(
                    self._active_position_aggregators.items()
                )
            },
            "finalized_position_ids": sorted(self._finalized_position_ids),
            "finalized_position_results": {
                position_id: dict(result)
                for position_id, result in sorted(
                    self._finalized_position_results.items()
                )
            },
            "legacy_fill_sequence": self._legacy_fill_sequence,
            "equity_unit": self._equity_unit,
            "equity_source": self._equity_source,
            "equity_scope": self._equity_scope,
            "max_drawdown_capability": self._max_drawdown_capability,
            "has_authoritative_oms_equity": self._has_authoritative_oms_equity,
        }
        if self.config.profile == RiskProfile.D_EXPERIMENT:
            option_d, reserved_intents = self._option_d.to_state()
            state["option_d"] = option_d
            state["reserved_intents"] = reserved_intents
        return state

    def load_state(self, state: dict) -> None:
        """Restore a runtime snapshot atomically from the caller's perspective."""
        previous = self.get_state()
        try:
            self._apply_state(state)
        except Exception:
            self._apply_state(previous)
            raise

    def _apply_state(self, state: dict) -> None:
        schema_version = state.get("schema_version")
        if self.config.profile == RiskProfile.D_EXPERIMENT:
            if schema_version != OPTION_D_STATE_VERSION:
                raise ValueError(
                    "option_d_runtime_state_version_invalid: "
                    f"expected={OPTION_D_STATE_VERSION}, actual={schema_version!r}"
                )
            if "option_d" not in state or "reserved_intents" not in state:
                raise ValueError("option_d_runtime_state_incomplete")
        elif schema_version == OPTION_D_STATE_VERSION or state.get("option_d") or state.get(
            "reserved_intents"
        ):
            raise ValueError("option_d_state_requires_d_experiment_profile")
        self._realized_points = float(state.get("realized_points", 0.0) or 0.0)
        self._peak_equity = float(state.get("peak_equity", 0.0) or 0.0)
        self._total_fills = int(state.get("total_fills", 0) or 0)
        self._daily_realized = float(state.get("daily_realized", 0.0) or 0.0)
        current_session_key = (
            state.get("current_risk_session")
            or state.get("current_session_key")
            or state.get("current_date")
        )
        self._current_session_key = (
            _normalize_session_key(current_session_key)
            if current_session_key
            else None
        )
        self._last_session_observed_at = _normalize_observed_at(
            state.get("last_session_observed_at")
        )
        self._session_source = state.get("session_source") or None
        self._consecutive_losses = int(
            state.get("true_consecutive_losses", state.get("consecutive_losses", 0))
            or 0
        )
        self._processed_close_fill_ids = {
            str(value) for value in state.get("processed_close_fill_ids", [])
        }
        self._active_position_aggregators = {}
        raw_aggregators = state.get("active_position_aggregators", {}) or {}
        for position_id, raw_state in raw_aggregators.items():
            aggregator_state = dict(raw_state or {})
            aggregator_state.setdefault("position_id", str(position_id))
            aggregator = PositionPnlAggregator.from_state(aggregator_state)
            self._active_position_aggregators[aggregator.position_id] = aggregator
        self._finalized_position_ids = {
            str(value) for value in state.get("finalized_position_ids", [])
        }
        self._finalized_position_results = {
            str(position_id): dict(result)
            for position_id, result in (
                state.get("finalized_position_results", {}) or {}
            ).items()
        }
        if not set(self._finalized_position_results).issubset(
            self._finalized_position_ids
        ):
            raise ValueError("finalized position result has no tombstone")
        active_fill_ids = {
            fill_id
            for aggregator in self._active_position_aggregators.values()
            for fill_id in aggregator.close_fill_ids
        }
        if not active_fill_ids.issubset(self._processed_close_fill_ids):
            raise ValueError("active position contains an unprocessed close fill")
        self._legacy_fill_sequence = int(state.get("legacy_fill_sequence", 0) or 0)
        option_d = state.get("option_d", {}) or {}
        reserved_intents = state.get("reserved_intents", {}) or {}
        self._option_d.load_state(option_d, reserved_intents)
        self._equity_unit = str(state.get("equity_unit") or "account_currency")
        self._equity_source = state.get("equity_source") or None
        self._equity_scope = state.get("equity_scope") or None
        self._max_drawdown_capability = (
            state.get("max_drawdown_capability") or None
        )
        self._has_authoritative_oms_equity = bool(
            state.get(
                "has_authoritative_oms_equity",
                self._equity_source == "oms_balance",
            )
        )

    def summary(self) -> str:
        """返回可读的风控状态摘要。"""
        lines = [
            "RiskManager Status:",
            f"  realized_points: {self._realized_points:.1f}",
            f"  peak_equity:     {self._peak_equity:.1f}",
            f"  total_fills:     {self._total_fills}",
        ]
        if self.config.daily_loss_limit is not None:
            lines.append(f"  daily_realized:  {self._daily_realized:.1f} "
                         f"(limit: {self.config.daily_loss_limit})")
        if self.config.max_consecutive_losses is not None:
            lines.append(f"  consecutive_losses: {self._consecutive_losses} "
                         f"(limit: {self.config.max_consecutive_losses})")
        if self.config.max_drawdown_pct is not None and self._peak_equity > 0:
            lines.append(f"  peak_equity:     {self._peak_equity:.1f}")
        return "\n".join(lines)


# ═══════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════


def _resolve_date(fill_time: object) -> object:
    """从各种时间类型中解析日期。"""
    if isinstance(fill_time, pd.Timestamp):
        return fill_time.date()
    if isinstance(fill_time, str):
        try:
            return pd.Timestamp(fill_time).date()
        except Exception:
            return fill_time
    if hasattr(fill_time, "date"):
        return fill_time.date()
    return fill_time


def _normalize_session_key(value: object) -> str:
    if value is None:
        raise ValueError("session_key must not be None")
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception as exc:
        raise ValueError(f"invalid session_key: {value!r}") from exc


def _normalize_observed_at(value: object | None) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        return pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"invalid observed_at: {value!r}") from exc


def _valid_equity(value: object | None) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _required_identity(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _finite_pnl(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid pnl_raw_points: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid pnl_raw_points: {value!r}")
    return number
