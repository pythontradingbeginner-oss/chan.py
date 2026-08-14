"""统一回测核心 —— 接受 StrategyConfig，运行完整回测。

替代过去散落在 5 个研究脚本中的重复回测循环。

架构：
  run_backtest(config) → BacktestResult
    └─ 每根 K 线：
       1. CChan.trigger_load(klu)
       2. RuntimeDecisionKernel.evaluate_bar() → TradeIntent
       3. 如果有仓位：ExitManager.check(ctx) → 优先出场
       4. 如果 graded.accepted：RiskManager.approve() → execute()
       5. 统一 trade tracking
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, BSP_TYPE, DATA_SRC, KL_TYPE

from .config import ExecutionParams, RiskParams, StrategyConfig
from .config_loader import make_exit_manager, make_runtime_decision_kernel
from .execution import Fill, SimulatedExecutionEngine
from .feed import prepare_ohlc_frame, row_to_klu
from .multi_level import MultiLevelDecisionContext
from .runtime_kernel import RuntimeDecisionKernel
from .risk import CloseFillRecord, RiskConfig, RiskManager
from .position_transition import PositionTransition, decompose_position_transition
from .option_d import probe_intent_identity
from .risk_session import RbRiskSessionResolver
from .strategy import StrategySignal
from signal_core.models import (
    SignalDecision,
    SignalDirection,
    SignalEvent,
    SignalState,
)
from strategy_policy.position import PositionContext
from strategy_policy.qingpai_decomposition import DecompositionTransition
from strategy_policy.exit_rules import ExitManager, ExitSignal
from strategy_policy.reporting import (
    StandardMetrics,
    compute_standard_metrics,
    save_standard_report,
)
from .trade_intent import DecisionTraceRecord


_TYPE_STR_TO_BSP: dict[str, BSP_TYPE] = {
    "1": BSP_TYPE.T1, "1p": BSP_TYPE.T1P,
    "2": BSP_TYPE.T2, "2s": BSP_TYPE.T2S,
    "3a": BSP_TYPE.T3A, "3b": BSP_TYPE.T3B,
}

_KL_TYPE_MAP: dict[str, KL_TYPE] = {
    "K_1M": KL_TYPE.K_1M, "K_5M": KL_TYPE.K_5M,
    "K_15M": KL_TYPE.K_15M, "K_30M": KL_TYPE.K_30M,
    "K_60M": KL_TYPE.K_60M, "K_DAY": KL_TYPE.K_DAY,
    "K_WEEK": KL_TYPE.K_WEEK, "K_MON": KL_TYPE.K_MON,
}


# ════════════════════════════════════════════════════════════════
# BacktestResult
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BacktestResult:
    """一次完整回测的所有产物。"""

    bars: pd.DataFrame            # 逐 bar 权益/仓位
    fills: pd.DataFrame           # 逐笔成交记录
    trades: list[dict]            # 逐笔交易 (open → close 配对)
    exit_events: pd.DataFrame     # ExitManager 触发出场事件
    exit_manager: ExitManager     # 异常审计接口
    config: StrategyConfig        # 回测使用的配置 (只读)
    signal_events: list = field(default_factory=list)  # 全量 SignalEvent
    signal_decisions: list = field(default_factory=list)  # 含 rejected 的全量决策
    decision_trace: list[DecisionTraceRecord] = field(default_factory=list)
    decomposition_transitions: list[DecompositionTransition] = field(default_factory=list)
    multi_level_audit: list[MultiLevelDecisionContext] = field(default_factory=list)
    execution_decisions: list[dict[str, Any]] = field(default_factory=list)

    def save(self, output_dir: Path | str) -> None:
        """保存标准化报告。"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        metrics = compute_standard_metrics(
            self.bars, self.fills, self.trades, self.exit_events,
            strategy_name="chan_backtest", variant=self.config.code,
            timeframe_minutes=_timeframe_minutes(self.config.kl_type),
        )
        save_standard_report(
            output_dir,
            bars=self.bars, fills=self.fills, trades=self.trades,
            exit_events=self.exit_events,
            config={
                "code": self.config.code,
                "kl_type": self.config.kl_type,
                "min_grade": self.config.grading.min_grade.value,
                "policy_mode": self.config.entry.get("policy_mode", "legacy"),
                "exit_rules": " + ".join(e.type for e in self.config.exits),
                "fee_points": self.config.execution.fee_points,
                "slippage_points": self.config.execution.slippage_points,
                "risk_profile": self.config.risk.profile.value,
                "risk_gate_modes": {
                    gate: mode.value
                    for gate, mode in self.config.risk.gate_modes().items()
                },
            },
            metrics=metrics,
        )
        if self.signal_decisions:
            pd.DataFrame([decision.to_dict() for decision in self.signal_decisions]).to_csv(
                output_dir / "signal_decisions.csv", index=False, encoding="utf-8-sig"
            )
        if self.decision_trace:
            pd.DataFrame([record.to_dict() for record in self.decision_trace]).to_csv(
                output_dir / "decision_trace.csv", index=False, encoding="utf-8-sig"
            )
        if self.decomposition_transitions:
            pd.DataFrame(
                [transition.to_dict() for transition in self.decomposition_transitions]
            ).to_csv(
                output_dir / "decomposition_transitions.csv",
                index=False,
                encoding="utf-8-sig",
            )
        if self.multi_level_audit:
            pd.DataFrame(
                [context.to_dict() for context in self.multi_level_audit]
            ).to_csv(
                output_dir / "multi_level_decisions.csv",
                index=False,
                encoding="utf-8-sig",
            )
        if self.execution_decisions:
            pd.DataFrame(self.execution_decisions).to_csv(
                output_dir / "execution_decisions.csv",
                index=False,
                encoding="utf-8-sig",
            )


# ════════════════════════════════════════════════════════════════
# 主回测函数
# ════════════════════════════════════════════════════════════════


def run_backtest(
    config: StrategyConfig,
    *,
    limit: int | None = None,
    frame: pd.DataFrame | None = None,
    parent_frame: pd.DataFrame | None = None,
    child_frame: pd.DataFrame | None = None,
    decision_start: object | None = None,
    decision_end: object | None = None,
) -> BacktestResult:
    """根据 StrategyConfig 运行一次完整回测。

    Args:
        config: 策略配置。
        limit: 限制回测的 bar 数量（用于快速调试）。
        frame: 可选，直接传入 DataFrame（跳过文件读取）。

    Returns:
        BacktestResult: 包含 bars、fills、trades、exit_events、exit_manager。
    """
    # ── 加载数据 ──
    if frame is None and config.production.enabled:
        from .production import load_rb_production_frames

        production_frames = load_rb_production_frames(config)
        frame = production_frames.current
        if parent_frame is None:
            parent_frame = production_frames.parent
        if child_frame is None:
            child_frame = production_frames.child

    if frame is not None:
        bars = prepare_ohlc_frame(frame)
    else:
        bars = prepare_ohlc_frame(pd.read_parquet(config.data_path))
    if limit is not None and limit > 0:
        bars = bars.iloc[:limit].reset_index(drop=True)

    kl_type = _resolve_kl_type(config.kl_type)

    # ── 构建 CChan ──
    chan = _new_chan(config, kl_type)

    # ── 三端共用决策内核 ──
    timeframe_str = kl_type.name.replace("K_", "").replace("M", "m")
    decision_kernel = make_runtime_decision_kernel(
        config,
        symbol="RB",
        timeframe=timeframe_str,
        parent_frame=(
            prepare_ohlc_frame(parent_frame)
            if parent_frame is not None
            else _load_level_frame(config.multi_level.parent_data_path)
            if config.multi_level.enabled
            else None
        ),
        child_frame=(
            prepare_ohlc_frame(child_frame)
            if child_frame is not None
            else _load_level_frame(config.multi_level.child_data_path)
            if config.multi_level.enabled
            else None
        ),
    )

    # ── 出场规则 ──
    exit_manager = make_exit_manager(config)
    use_exit_rules = len(config.exits) > 0

    # ── 风控 + 执行 + 仓位 ──
    risk = RiskManager(
        RiskConfig(
            max_abs_position=config.risk.max_abs_position,
            max_loss_points=config.risk.max_loss_points,
            daily_loss_limit=config.risk.daily_loss_limit,
            max_consecutive_losses=config.risk.max_consecutive_losses,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            require_session_key=config.risk.daily_loss_limit is not None,
            profile=config.risk.profile,
            max_loss_points_mode=config.risk.max_loss_points_mode,
            daily_loss_limit_mode=config.risk.daily_loss_limit_mode,
            max_consecutive_losses_mode=config.risk.max_consecutive_losses_mode,
            max_drawdown_pct_mode=config.risk.max_drawdown_pct_mode,
        ),
        initial_equity=config.sizing.capital,
        initial_equity_source="backtest_mark_to_market",
        equity_scope="simulated_account",
        max_drawdown_capability=(
            "enabled"
            if config.risk.max_drawdown_pct is not None
            else "disabled_by_design"
        ),
    )
    execution = SimulatedExecutionEngine(
        fee_points=config.execution.fee_points,
        slippage_points=config.execution.slippage_points,
        price_tick=config.execution.price_tick,
    )

    # ── 过滤器 (DC 结构 + OBV + 成交量) ──
    filter_ctx = None
    use_filters = config.filter.enabled
    if use_filters:
        from .filters import FilterPipeline
        pipeline = FilterPipeline(
            dc_threshold_points=config.filter.dc_threshold_points,
            obv_window=config.filter.obv_window,
            volume_window=config.filter.volume_window,
            volume_strength=config.filter.volume_strength,
        )
        filter_ctx = pipeline.precompute(bars)

    # ── 回测循环 ──
    (
        records,
        fills_df,
        trades,
        exit_events,
        signal_events,
        signal_decisions,
        decision_trace,
        decomposition_transitions,
        multi_level_audit,
        execution_decisions,
    ) = _run_loop(
        bars=bars,
        chan=chan,
        kl_type=kl_type,
        decision_kernel=decision_kernel,
        exit_manager=exit_manager,
        use_exit_rules=use_exit_rules,
        risk=risk,
        execution=execution,
        config=config,
        filter_ctx=filter_ctx,
        decision_start=decision_start,
        decision_end=decision_end,
    )

    equity = pd.DataFrame(records)
    fills_df_final = pd.DataFrame([f.__dict__ for f in execution.fills])
    exit_evts_df = pd.DataFrame(exit_events)

    return BacktestResult(
        bars=equity,
        fills=fills_df_final,
        trades=trades,
        exit_events=exit_evts_df,
        exit_manager=exit_manager,
        config=config,
        signal_events=signal_events,
        signal_decisions=signal_decisions,
        decision_trace=decision_trace,
        decomposition_transitions=decomposition_transitions,
        multi_level_audit=multi_level_audit,
        execution_decisions=execution_decisions,
    )


# ════════════════════════════════════════════════════════════════
# 内部：回测主循环
# ════════════════════════════════════════════════════════════════


def _run_loop(
    *,
    bars: pd.DataFrame,
    chan: CChan,
    kl_type: KL_TYPE,
    decision_kernel: RuntimeDecisionKernel,
    exit_manager: ExitManager,
    use_exit_rules: bool,
    risk: RiskManager,
    execution: SimulatedExecutionEngine,
    config: StrategyConfig,
    filter_ctx: object | None = None,
    decision_start: object | None = None,
    decision_end: object | None = None,
) -> tuple[
    list[dict],
    pd.DataFrame,
    list[dict],
    list[dict],
    list[SignalEvent],
    list[SignalDecision],
    list[DecisionTraceRecord],
    list[DecompositionTransition],
    list[MultiLevelDecisionContext],
    list[dict[str, Any]],
]:
    """每根 K 线的主循环。"""

    use_filters = filter_ctx is not None and config.filter.enabled
    fee_points = config.execution.fee_points
    records: list[dict] = []
    exit_events: list[dict] = []
    trades: list[dict] = []
    position_context: PositionContext | None = None
    atr_values = _calculate_atr(bars, config.sizing.atr_period)
    amplitude_values = (bars["high"] - bars["low"]).abs().rolling(
        window=20,
        min_periods=5,
    ).mean()
    _bar_events: list[SignalEvent] = []
    _event_ids: set[str] = set()
    _decisions: list[SignalDecision] = []
    execution_decisions: list[dict[str, Any]] = []
    setup_attempt_counts: dict[str, int] = {}
    setup_prior_exit_reasons: dict[str, str] = {}
    position_close_aggregates: dict[str, dict[str, Any]] = {}
    probe_position_epochs: dict[str, str] = {}
    extractor = decision_kernel.extractor
    previous_active_symbol: str | None = None
    risk_session_resolver = (
        RbRiskSessionResolver()
        if config.risk.daily_loss_limit is not None
        else None
    )

    def _resolve_risk_session(
        observed_at: object,
        *,
        source: str,
        explicit_trading_day: object | None = None,
    ):
        if risk_session_resolver is None:
            return None
        return risk_session_resolver.resolve(
            observed_at,
            source=source,
            explicit_trading_day=explicit_trading_day,
        )

    def _record_execution_decision(
        *,
        intent,
        signal: StrategySignal,
        row_number: int,
        timestamp,
        status: str,
        rejection_stage: str = "",
        rejection_reason: str = "",
        risk_approved: bool | None = None,
        risk_reason: str = "",
        fill: Fill | None = None,
        previous_position: int | None = None,
        current_equity: float | None = None,
        current_equity_points: float | None = None,
        risk_decision=None,
        transition: PositionTransition | None = None,
    ) -> None:
        before_position = (
            execution.state.position
            if previous_position is None
            else previous_position
        )
        quantity_delta = signal.target_position - before_position
        risk_state = risk.get_state()
        option_d_state = risk_state.get("option_d", {}) or {}
        execution_decisions.append(
            {
                "timestamp": timestamp,
                "row_number": row_number,
                "event_id": intent.event_id,
                "signal_key": intent.signal_key,
                "decision_id": intent.decision.decision_id,
                "policy_id": intent.decision.policy_id,
                "rule_accepted": intent.accepted,
                "status": status,
                "rejection_stage": rejection_stage,
                "rejection_reason": rejection_reason,
                "risk_approved": risk_approved,
                "risk_reason": risk_reason,
                "risk_profile": config.risk.profile.value,
                "risk_gate_modes": json.dumps(
                    {
                        gate: mode.value
                        for gate, mode in config.risk.gate_modes().items()
                    },
                    sort_keys=True,
                ),
                "risk_hard_failures": json.dumps(
                    list(risk_decision.hard_failures) if risk_decision else []
                ),
                "risk_enforced_breaches": json.dumps(
                    list(risk_decision.enforced_breaches) if risk_decision else []
                ),
                "risk_observed_breaches": json.dumps(
                    list(risk_decision.observed_breaches) if risk_decision else []
                ),
                "risk_evaluated_gates": json.dumps(
                    list(risk_decision.evaluated_gates) if risk_decision else []
                ),
                "transition_is_reversal": (
                    transition.is_reversal if transition is not None else False
                ),
                "transition_has_close_leg": (
                    transition.close_leg is not None if transition is not None else False
                ),
                "transition_has_open_leg": (
                    transition.open_leg is not None if transition is not None else False
                ),
                "action": signal.action,
                "direction": _signal_direction(signal),
                "bsp_type": signal.bsp_type,
                "active_symbol": signal.active_symbol or active_symbol_str or "",
                "signal_price": signal.price,
                "previous_position": before_position,
                "target_position": signal.target_position,
                "quantity_delta": quantity_delta,
                "fill_price": fill.fill_price if fill is not None else None,
                "fill_target_position": fill.target_position if fill is not None else None,
                "fill_quantity_delta": fill.quantity_delta if fill is not None else None,
                "fill_realized_points": fill.realized_points if fill is not None else None,
                "fill_equity_points": fill.equity_points if fill is not None else None,
                "current_equity": current_equity,
                "current_account_equity": current_equity,
                "current_equity_points": current_equity_points,
                "risk_realized_points": risk_state.get("realized_points"),
                "risk_daily_realized": risk_state.get("daily_realized"),
                "risk_consecutive_losses": risk_state.get("consecutive_losses"),
                "risk_peak_equity": risk_state.get("peak_equity"),
                "risk_total_fills": risk_state.get("total_fills"),
                "option_d_regime_state": option_d_state.get("regime_state", ""),
                "option_d_audit_state": (
                    "PROBE_PENDING_ENTRY"
                    if option_d_state.get("regime_state") == "PROBE_AVAILABLE"
                    and option_d_state.get("reserved_intent_id")
                    else option_d_state.get("regime_state", "")
                ),
                "option_d_probe_epoch_id": option_d_state.get(
                    "probe_epoch_id", ""
                ),
                "option_d_probe_position_id": option_d_state.get(
                    "probe_position_id", ""
                ),
                "risk_session_key": risk_state.get("current_session_key"),
                "risk_session_observed_at": risk_state.get("last_session_observed_at"),
                "risk_session_source": risk_state.get("session_source"),
                "equity_unit": "account_currency",
                "equity_source": "backtest_mark_to_market",
                "equity_scope": "simulated_account",
                "max_drawdown_capability": (
                    "enabled"
                    if config.risk.max_drawdown_pct is not None
                    else "disabled_by_design"
                ),
                **intent.audit_identity_fields(),
            }
        )

    def _record_risk_close_fill(
        *,
        context: PositionContext,
        pnl: float,
        exit_bar: int,
        exit_time: object,
        exit_price: float,
        remaining_quantity: int,
        identity_suffix: str | None = None,
    ) -> dict[str, str]:
        close_identity = _backtest_close_identity_fields(
            context.position_id,
            exit_bar,
            identity_suffix=identity_suffix,
        )
        risk_session = _resolve_risk_session(
            exit_time,
            source="backtest_fill",
        )
        risk.record_close_fill(
            CloseFillRecord(
                fill_id=close_identity["close_fill_id"],
                order_id=close_identity["close_order_id"],
                position_id=context.position_id,
                pnl_raw_points=pnl,
                risk_session_key=(
                    risk_session.session_key if risk_session is not None else None
                ),
                observed_at=exit_time,
                source="backtest_fill",
                current_equity=_account_equity(config, execution, exit_price),
                equity_source="backtest_mark_to_market",
                equity_scope="simulated_account",
                max_drawdown_capability=(
                    "enabled"
                    if config.risk.max_drawdown_pct is not None
                    else "disabled_by_design"
                ),
            ),
            remaining_quantity=remaining_quantity,
        )
        return close_identity

    def _record_partial_close(
        *,
        context: PositionContext,
        fill_volume: int,
        exit_bar: int,
        exit_price: float,
        exit_time: object,
    ) -> None:
        position_id = context.position_id
        aggregate = position_close_aggregates.setdefault(
            position_id,
            {
                "raw_pnl_points": 0.0,
                "close_fill_ids": [],
                "closed_volume": 0,
            },
        )
        pnl = context.pnl_points(
            exit_price=exit_price,
            fee_points=fee_points,
            volume=fill_volume,
        )
        close_identity = _record_risk_close_fill(
            context=context,
            pnl=pnl,
            exit_bar=exit_bar,
            exit_time=exit_time,
            exit_price=exit_price,
            remaining_quantity=context.volume - fill_volume,
            identity_suffix=f"partial-{len(aggregate['close_fill_ids']) + 1}",
        )
        aggregate["raw_pnl_points"] += pnl
        aggregate["close_fill_ids"].append(close_identity["close_fill_id"])
        aggregate["closed_volume"] += fill_volume

    def _close_trade(
        exit_bar: int, exit_price: float, exit_time,
        reason: str, rule_id: str = "strategy_reverse",
    ):
        nonlocal position_context
        if position_context is None:
            return
        entry_bar = position_context.entry_bar
        final_fill_pnl = position_context.pnl_points(
            exit_price=exit_price,
            fee_points=fee_points,
        )
        setup_candidate_id = position_context.setup_candidate_id
        attempt_sequence = setup_attempt_counts.get(setup_candidate_id, 0) + 1
        prior_exit_reason = setup_prior_exit_reasons.get(setup_candidate_id)
        position_id = position_context.position_id
        aggregate = position_close_aggregates.pop(
            position_id,
            {
                "raw_pnl_points": 0.0,
                "close_fill_ids": [],
                "closed_volume": 0,
            },
        )
        pnl = float(aggregate["raw_pnl_points"]) + final_fill_pnl
        close_identity = _record_risk_close_fill(
            context=position_context,
            pnl=final_fill_pnl,
            exit_bar=exit_bar,
            exit_time=exit_time,
            exit_price=exit_price,
            remaining_quantity=0,
        )
        close_fill_ids = [
            *aggregate["close_fill_ids"],
            close_identity["close_fill_id"],
        ]
        trade = {
            "entry_bar": entry_bar,
            "entry_price": position_context.entry_price,
            "entry_time": position_context.entry_time,
            "direction": position_context.direction.value,
            "lots": int(aggregate["closed_volume"]) + position_context.volume,
            "grade": position_context.entry_grade,
            "active_symbol": position_context.active_symbol or "",
            "event_id": position_context.event_id,
            "signal_key": position_context.signal_key,
            "decision_id": position_context.decision_id,
            **close_identity,
            "close_fill_ids": json.dumps(close_fill_ids),
            "setup_invalidation_price": position_context.setup_invalidation_price,
            "execution_stop_price": position_context.execution_stop_price,
            "setup_candidate_schema_version": position_context.setup_candidate_schema_version,
            "setup_candidate_id": setup_candidate_id,
            "setup_contract_epoch": position_context.setup_contract_epoch,
            "setup_timeframe": position_context.setup_timeframe,
            "setup_direction": position_context.setup_direction,
            "setup_root_bi_idx": position_context.setup_root_bi_idx,
            "setup_family": position_context.setup_family,
            "setup_state": position_context.setup_state,
            "attempt_sequence": attempt_sequence,
            "prior_exit_reason": prior_exit_reason,
            "is_option_d_probe": position_id in probe_position_epochs,
            "option_d_probe_epoch_id": probe_position_epochs.pop(position_id, ""),
            "exit_bar": exit_bar, "exit_price": exit_price,
            "exit_time": exit_time,
            "hold_bars": exit_bar - (entry_bar if entry_bar is not None else exit_bar),
            "pnl_points": round(pnl, 1),
            "exit_reason": reason, "exit_rule": rule_id,
        }
        trades.append(trade)
        setup_attempt_counts[setup_candidate_id] = attempt_sequence
        setup_prior_exit_reasons[setup_candidate_id] = reason
        risk.finalize_position(
            position_id,
            finalized_at=exit_time,
            reason=reason,
        )
        position_context = None

    for row_number, row in bars.iterrows():
        timestamp = row["datetime"]
        if isinstance(timestamp, pd.Timestamp):
            dt_timestamp = timestamp.to_pydatetime()
        else:
            dt_timestamp = timestamp
        active_symbol = row.get("active_symbol")
        active_symbol_str = str(active_symbol) if pd.notna(active_symbol) else None
        exit_signal: ExitSignal | None = None

        rollover = (
            config.production.enabled
            and previous_active_symbol is not None
            and active_symbol_str is not None
            and active_symbol_str != previous_active_symbol
        )
        if rollover:
            if (
                config.production.close_on_rollover
                and position_context is not None
                and execution.state.position != 0
            ):
                previous_row = bars.iloc[row_number - 1]
                rollover_price = _execution_ohlc(previous_row, config)[3]
                rollover_time = previous_row["datetime"]
                rollover_signal = StrategySignal(
                    timestamp=rollover_time,
                    action=(
                        "close_long"
                        if execution.state.position > 0
                        else "close_short"
                    ),
                    target_position=0,
                    price=rollover_price,
                    reason="contract_rollover",
                    bsp_type="exit",
                    bsp_bi_idx=-1,
                    bsp_klu_idx=-1,
                    active_symbol=previous_active_symbol,
                )
                fill = execution.execute(rollover_signal)
                executed_exit = (
                    fill.fill_price if fill is not None else rollover_price
                )
                _close_trade(
                    exit_bar=row_number - 1,
                    exit_price=executed_exit,
                    exit_time=rollover_time,
                    reason="contract_rollover",
                    rule_id="contract_rollover",
                )
                exit_manager.on_close()
                exit_events.append(
                    {
                        "row_number": row_number - 1,
                        "datetime": rollover_time,
                        "rule_id": "contract_rollover",
                        "reason_code": "contract_rollover",
                        "trigger_price": rollover_price,
                        "fill_price": executed_exit,
                        "description": (
                            f"{previous_active_symbol} -> {active_symbol_str} 换月平仓"
                        ),
                    }
                )
                if records:
                    records[-1].update(
                        {
                            "position": 0,
                            "avg_price": None,
                            "realized_points": execution.state.realized_points,
                            "equity_points": execution.mark_to_market(rollover_price),
                            "account_equity": _account_equity(
                                config,
                                execution,
                                rollover_price,
                            ),
                            "exit_reason": "contract_rollover",
                        }
                    )
            if config.production.reset_structure_on_rollover:
                chan = _new_chan(config, kl_type)
                decision_kernel.reset_contract_state(
                    start_time=timestamp,
                    active_symbol=active_symbol_str,
                )

        if config.production.enabled and active_symbol_str is not None:
            extractor.contract = active_symbol_str

        current_risk_session = _resolve_risk_session(
            timestamp,
            source="backtest_bar",
            explicit_trading_day=row.get("trading_day"),
        )
        if current_risk_session is not None:
            risk.advance_session(
                current_risk_session.session_key,
                observed_at=current_risk_session.observed_at,
                source=current_risk_session.source,
            )

        klu = row_to_klu(row, kl_type=kl_type)
        chan.trigger_load({kl_type: [klu]})
        adjusted_price = float(row["close"])
        exec_open, exec_high, exec_low, price = _execution_ohlc(row, config)
        price_adjustment = _price_adjustment(row, config)
        decomposition = decision_kernel.observe_structure(
            chan=chan,
            timestamp=timestamp,
            lv_idx=0,
        )

        equity_points = execution.mark_to_market(price)
        equity_cash = _account_equity(config, execution, price)
        risk.observe_equity(
            equity_cash,
            source="backtest_mark_to_market",
            scope="simulated_account",
            max_drawdown_capability=(
                "enabled"
                if config.risk.max_drawdown_pct is not None
                else "disabled_by_design"
            ),
        )
        used_margin = (
            abs(execution.state.position)
            * price
            * config.execution.contract_multiplier
            * config.execution.margin_rate
        )
        available_funds = max(0.0, equity_cash - used_margin)
        in_decision_window = _in_decision_window(
            timestamp,
            start=decision_start,
            end=decision_end,
        )

        evaluated = None
        if in_decision_window or execution.state.position != 0:
            evaluated = decision_kernel.evaluate_bar(
                chan=chan,
                current_position=execution.state.position,
                price=price,
                timestamp=timestamp,
                active_symbol=active_symbol_str,
                lv_idx=0,
                account_equity=equity_cash,
                available_funds=available_funds,
                atr=_finite_or_none(atr_values.iloc[row_number]),
                price_adjustment=price_adjustment,
                risk_session_key=(
                    current_risk_session.session_key
                    if current_risk_session is not None
                    else None
                ),
                risk_session_observed_at=(
                    current_risk_session.observed_at
                    if current_risk_session is not None
                    else None
                ),
                risk_session_source=(
                    current_risk_session.source
                    if current_risk_session is not None
                    else ""
                ),
                equity_source="backtest_mark_to_market",
                equity_scope="simulated_account",
                max_drawdown_capability=(
                    "enabled"
                    if config.risk.max_drawdown_pct is not None
                    else "disabled_by_design"
                ),
            )
            if evaluated is not None:
                _decisions.append(evaluated.decision)
                if (
                    evaluated.event is not None
                    and evaluated.event.event_id not in _event_ids
                ):
                    _bar_events.append(evaluated.event)
                    _event_ids.add(evaluated.event.event_id)

        holding_direction = (
            "long" if execution.state.position >= 0 else "short"
        )
        chan_exit_snapshot = decision_kernel.build_exit_snapshot(
            chan,
            observed_at=timestamp,
            direction=holding_direction,
            open_price=exec_open,
            high_price=exec_high,
            low_price=exec_low,
            close_price=price,
            average_amplitude=_finite_or_none(amplitude_values.iloc[row_number]),
            price_adjustment=price_adjustment,
            lv_idx=0,
        )

        # ── 1. 按固定优先级检查出场规则 ──
        if use_exit_rules and exit_manager.is_active:
            exit_signal = exit_manager.check(
                bar_end_time=dt_timestamp,
                open=exec_open,
                high=exec_high,
                low=exec_low,
                close=price,
                opposite_signal_triggered=_is_confirmed_opposite(
                    evaluated,
                    execution.state.position,
                ),
                chan_snapshot=chan_exit_snapshot,
            )
            if exit_signal is not None and execution.state.position != 0:
                # 强制平仓
                target = 0
                action = "close_long" if execution.state.position > 0 else "close_short"
                sig = StrategySignal(
                    timestamp=timestamp, action=action, target_position=target,
                    price=exit_signal.exit_price, reason=exit_signal.reason_code,
                    bsp_type="exit", bsp_bi_idx=-1, bsp_klu_idx=-1,
                    active_symbol=active_symbol_str,
                )
                fill = execution.execute(sig)
                executed_exit = fill.fill_price if fill is not None else exit_signal.exit_price
                _close_trade(
                    exit_bar=row_number,
                    exit_price=executed_exit,
                    exit_time=timestamp,
                    reason=exit_signal.reason_code,
                    rule_id=exit_signal.rule_id,
                )
                exit_manager.on_close()
                exit_events.append({
                    "row_number": row_number, "datetime": timestamp,
                    "rule_id": exit_signal.rule_id,
                    "reason_code": exit_signal.reason_code,
                    "trigger_price": exit_signal.exit_price,
                    "fill_price": executed_exit,
                    "description": exit_signal.description,
                })

        # ── 2. 仅在样本外决策窗口内执行新入场/反转 ──
        if exit_signal is not None and evaluated is not None and evaluated.accepted:
            _record_execution_decision(
                intent=evaluated,
                signal=evaluated.signal,
                row_number=row_number,
                timestamp=timestamp,
                status="blocked_by_exit_priority",
                rejection_stage="exit_priority",
                rejection_reason=exit_signal.reason_code,
                current_equity=equity_cash,
                current_equity_points=equity_points,
            )
        elif not in_decision_window and evaluated is not None and evaluated.accepted:
            _record_execution_decision(
                intent=evaluated,
                signal=evaluated.signal,
                row_number=row_number,
                timestamp=timestamp,
                status="outside_decision_window",
                rejection_stage="decision_window",
                rejection_reason="outside_decision_window",
                current_equity=equity_cash,
                current_equity_points=equity_points,
            )
        elif in_decision_window:
            graded = evaluated if evaluated is not None and evaluated.accepted else None
            signal = graded.signal if graded is not None else None

            if signal is not None:
                original_signal = signal
                # ── 0. 过滤器检查 (DC + OBV + Volume) ──
                if filter_ctx is not None and use_filters:
                    action = signal.action
                    # 仅在开仓/反转时检查过滤条件（平仓信号不拦截）
                    if action in ("open_long", "reverse_short_to_long"):
                        from .filters import FilterPipeline
                        pipeline = FilterPipeline()
                        if not pipeline.check_long(filter_ctx, row_number):
                            _record_execution_decision(
                                intent=graded,
                                signal=original_signal,
                                row_number=row_number,
                                timestamp=timestamp,
                                status="filter_rejected",
                                rejection_stage="filter",
                                rejection_reason="long_filter_rejected",
                                current_equity=equity_cash,
                                current_equity_points=equity_points,
                            )
                            signal = None  # 过滤掉做多信号
                    elif action in ("open_short", "reverse_long_to_short"):
                        from .filters import FilterPipeline
                        pipeline = FilterPipeline()
                        if not pipeline.check_short(filter_ctx, row_number):
                            _record_execution_decision(
                                intent=graded,
                                signal=original_signal,
                                row_number=row_number,
                                timestamp=timestamp,
                                status="filter_rejected",
                                rejection_stage="filter",
                                rejection_reason="short_filter_rejected",
                                current_equity=equity_cash,
                                current_equity_points=equity_points,
                            )
                            signal = None  # 过滤掉做空信号

                if signal is not None:
                    previous_position = execution.state.position
                    transition = decompose_position_transition(
                        previous_position,
                        signal.target_position,
                    )
                    close_fill = None
                    if transition.close_leg is not None:
                        close_signal = replace(
                            signal,
                            action=(
                                "close_long"
                                if previous_position > 0
                                else "close_short"
                            ),
                            target_position=transition.close_leg.target_position,
                        )
                        close_fill = execution.execute(close_signal)
                        if transition.close_leg.target_position == 0:
                            close_price = (
                                close_fill.fill_price
                                if close_fill is not None
                                else signal.price
                            )
                            _close_trade(
                                exit_bar=row_number,
                                exit_price=close_price,
                                exit_time=timestamp,
                                reason=(
                                    "strategy_reverse"
                                    if transition.is_reversal
                                    else "strategy_close"
                                ),
                                rule_id=(
                                    "strategy_reversal"
                                    if transition.is_reversal
                                    else "strategy_close"
                                ),
                            )
                            exit_manager.on_close()
                        elif position_context is not None:
                            partial_price = (
                                close_fill.fill_price
                                if close_fill is not None
                                else signal.price
                            )
                            _record_partial_close(
                                context=position_context,
                                fill_volume=transition.close_leg.quantity,
                                exit_bar=row_number,
                                exit_price=partial_price,
                                exit_time=timestamp,
                            )
                            position_context = position_context.reduce_volume(
                                transition.close_leg.quantity
                            )
                            if position_context is not None:
                                exit_manager.on_position_updated(position_context)

                    post_close_equity_points = execution.mark_to_market(price)
                    post_close_equity = _account_equity(config, execution, price)
                    approval_signal = (
                        replace(
                            signal,
                            action=(
                                "open_long"
                                if signal.target_position > 0
                                else "open_short"
                            ),
                        )
                        if transition.open_leg is not None
                        else signal
                    )
                    decision = risk.approve(
                        approval_signal,
                        current_equity=post_close_equity,
                        before_position=(
                            transition.open_leg.before_position
                            if transition.open_leg is not None
                            else previous_position
                        ),
                    )
                    if not decision.approved:
                        _record_execution_decision(
                            intent=graded,
                            signal=signal,
                            row_number=row_number,
                            timestamp=timestamp,
                            status="risk_rejected",
                            rejection_stage="risk",
                            rejection_reason=decision.reason,
                            risk_approved=False,
                            risk_reason=decision.reason,
                            previous_position=previous_position,
                            current_equity=post_close_equity,
                            current_equity_points=post_close_equity_points,
                            risk_decision=decision,
                            transition=transition,
                        )
                    else:
                        probe_intent_id = ""
                        probe_epoch_id = ""
                        if (
                            transition.open_leg is not None
                            and risk.option_d_probe_available
                        ):
                            open_order_intent = graded.with_signal(
                                approval_signal
                            ).for_open_leg(transition.open_leg.quantity)
                            open_order_signal = open_order_intent.signal
                            option_state = risk.get_state()["option_d"]
                            probe_epoch_id = str(option_state["probe_epoch_id"])
                            probe_intent_id = probe_intent_identity(
                                probe_epoch_id,
                                graded.event_id,
                            )
                            risk.reserve_probe_intent(
                                intent_id=probe_intent_id,
                                decision_id=graded.decision.decision_id,
                                event_id=graded.event_id,
                                target_position=open_order_signal.target_position,
                                planned_volume=transition.open_leg.quantity,
                                intent_snapshot=open_order_intent.to_runtime_snapshot(),
                            )
                        fill = (
                            execution.execute(approval_signal)
                            if transition.open_leg is not None
                            else close_fill
                        )
                        if fill is None:
                            if probe_intent_id:
                                risk.release_probe_reservation(probe_intent_id)
                            _record_execution_decision(
                                intent=graded,
                                signal=signal,
                                row_number=row_number,
                                timestamp=timestamp,
                                status="no_position_change",
                                rejection_stage="execution",
                                rejection_reason="quantity_delta_zero",
                                risk_approved=True,
                                risk_reason=decision.reason,
                                previous_position=previous_position,
                                current_equity=post_close_equity,
                                current_equity_points=post_close_equity_points,
                                risk_decision=decision,
                                transition=transition,
                            )
                        else:
                            if transition.open_leg is not None and fill.target_position != 0:
                                fill_volume = abs(fill.quantity_delta)
                                if position_context is None:
                                    position_context = graded.position_from_fill(
                                        fill_price=fill.fill_price,
                                        fill_volume=fill_volume,
                                        fill_time=timestamp,
                                        entry_bar=row_number,
                                        active_symbol=active_symbol_str,
                                    )
                                    exit_manager.on_position_opened(position_context)
                                else:
                                    position_context = position_context.merge_open_fill(
                                        fill_price=fill.fill_price,
                                        fill_volume=fill_volume,
                                        fill_time=timestamp,
                                    )
                                    exit_manager.on_position_updated(position_context)
                                if probe_intent_id:
                                    probe_order_id = f"backtest-open-{position_context.position_id}"
                                    risk.bind_probe_orders(
                                        probe_intent_id,
                                        [probe_order_id],
                                    )
                                    risk.record_probe_open_fill(
                                        intent_id=probe_intent_id,
                                        position_id=position_context.position_id,
                                        fill_volume=fill_volume,
                                        order_id=probe_order_id,
                                    )
                                    probe_position_epochs[
                                        position_context.position_id
                                    ] = probe_epoch_id
                            _record_execution_decision(
                                intent=graded,
                                signal=signal,
                                row_number=row_number,
                                timestamp=timestamp,
                                status=(
                                    "executed_reversal"
                                    if transition.is_reversal
                                    else _executed_status(previous_position, fill)
                                ),
                                risk_approved=True,
                                risk_reason=decision.reason,
                                fill=fill,
                                previous_position=previous_position,
                                current_equity=post_close_equity,
                                current_equity_points=post_close_equity_points,
                                risk_decision=decision,
                                transition=transition,
                            )

        # ── 3. 记录 bar-level 快照 ──
        records.append({
            "row_number": row_number, "datetime": timestamp,
            "close": adjusted_price,
            "execution_close": price,
            "active_symbol": active_symbol_str,
            "position": execution.state.position,
            "avg_price": execution.state.avg_price,
            "realized_points": execution.state.realized_points,
            "equity_points": execution.mark_to_market(price),
            "account_equity": _account_equity(config, execution, price),
            "risk_session_key": risk.get_state().get("current_session_key"),
            "exit_reason": exit_signal.reason_code if exit_signal else None,
            "decomposition_id": (
                decomposition.decomposition_id if decomposition else None
            ),
            "decomposition_revision": (
                decomposition.revision if decomposition else None
            ),
            "qingpai_regime": decomposition.regime.value if decomposition else None,
            "qingpai_direction": (
                decomposition.direction.value if decomposition else None
            ),
            "decomposition_lifecycle": (
                decomposition.lifecycle.value if decomposition else None
            ),
        })
        previous_active_symbol = active_symbol_str or previous_active_symbol

        # ── 4. 信号提取 + 持久化 (每 bar 都提取, 供后续信号分析) ──
        if extractor is not None:
            chan_snap = chan
            bsp_list = chan_snap[0].bs_point_lst if chan_snap else None
            if bsp_list is not None:
                for bsp in bsp_list.bsp_iter():
                    evt = extractor.extract(bsp, chan=chan_snap, bar_end_time=dt_timestamp, lv_idx=0)
                    if evt is not None and evt.event_id not in _event_ids:
                        _bar_events.append(evt)
                        _event_ids.add(evt.event_id)

    if position_context is not None and execution.state.position != 0 and records:
        last_bar = bars.iloc[-1]
        exit_price = _execution_ohlc(last_bar, config)[3]
        action = (
            "close_long"
            if position_context.direction == SignalDirection.LONG
            else "close_short"
        )
        end_signal = StrategySignal(
            timestamp=last_bar["datetime"],
            action=action,
            target_position=0,
            price=exit_price,
            reason="end_of_data",
            bsp_type="exit",
            bsp_bi_idx=-1,
            bsp_klu_idx=-1,
            active_symbol=position_context.active_symbol,
        )
        fill = execution.execute(end_signal)
        executed_exit = fill.fill_price if fill is not None else exit_price
        _close_trade(
            exit_bar=len(bars) - 1,
            exit_price=executed_exit,
            exit_time=last_bar["datetime"],
            reason="end_of_data",
            rule_id="end_of_data",
        )
        exit_manager.on_close()
        records[-1].update(
            {
                "position": execution.state.position,
                "avg_price": execution.state.avg_price,
                "realized_points": execution.state.realized_points,
                "equity_points": execution.mark_to_market(exit_price),
                "account_equity": _account_equity(config, execution, exit_price),
                "exit_reason": "end_of_data",
            }
        )

    return (
        records,
        pd.DataFrame(),
        trades,
        exit_events,
        _bar_events,
        _decisions,
        list(decision_kernel.decision_trace),
        list(decision_kernel.decomposition_transitions),
        list(decision_kernel.multi_level_audit),
        execution_decisions,
    )


# ════════════════════════════════════════════════════════════════
# 内部辅助
# ════════════════════════════════════════════════════════════════


def _new_chan(config: StrategyConfig, kl_type: KL_TYPE) -> CChan:
    """从配置构建 CChan 实例。"""
    chan_cfg = config.chan.to_dict()
    return CChan(
        code=config.code,
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[kl_type],
        config=CChanConfig(chan_cfg),
        autype=AUTYPE.NONE,
    )


def _load_level_frame(path: str | None) -> pd.DataFrame | None:
    if not path:
        return None
    return prepare_ohlc_frame(pd.read_parquet(path))


def _resolve_kl_type(kl_type_str: str) -> KL_TYPE:
    """将字符串 K 线级别映射到 KL_TYPE 枚举。"""
    try:
        return _KL_TYPE_MAP[kl_type_str]
    except KeyError:
        available = ", ".join(sorted(_KL_TYPE_MAP))
        raise ValueError(f"未知 K 线级别: {kl_type_str}。可用: {available}")


def _resolve_bsp_types(types: list[str] | None) -> list[BSP_TYPE] | None:
    """将字符串 BSP 类型列表映射到 BSP_TYPE 枚举。"""
    if types is None:
        return None
    return [_TYPE_STR_TO_BSP[t] for t in types if t in _TYPE_STR_TO_BSP]


def _timeframe_minutes(kl_type_str: str) -> int:
    """从 K 线级别字符串提取分钟数。"""
    mapping = {"K_1M": 1, "K_5M": 5, "K_15M": 15,
               "K_30M": 30, "K_60M": 60, "K_DAY": 1440}
    return mapping.get(kl_type_str, 15)


def _calculate_atr(bars: pd.DataFrame, period: int) -> pd.Series:
    """只使用当前及历史已收盘 K 线计算简单 ATR。"""
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def _finite_or_none(value: object) -> float | None:
    if pd.isna(value):
        return None
    return float(value)


def _backtest_close_identity_fields(
    position_id: str,
    exit_bar: int,
    *,
    identity_suffix: str | None = None,
) -> dict[str, str]:
    normalized_position_id = str(position_id).strip()
    if not normalized_position_id:
        raise ValueError("position_id is required for backtest close identity")
    suffix = f"{normalized_position_id}:{int(exit_bar)}"
    if identity_suffix:
        suffix = f"{suffix}:{identity_suffix}"
    return {
        "position_id": normalized_position_id,
        "close_fill_id": f"backtest-fill:{suffix}",
        "close_order_id": f"backtest-order:{suffix}",
    }


def _execution_ohlc(
    row: pd.Series,
    config: StrategyConfig,
) -> tuple[float, float, float, float]:
    use_raw = (
        config.production.enabled
        and config.production.use_raw_execution_prices
        and all(f"raw_{column}" in row.index for column in ("open", "high", "low", "close"))
    )
    prefix = "raw_" if use_raw else ""
    return tuple(
        float(row[f"{prefix}{column}"])
        for column in ("open", "high", "low", "close")
    )  # type: ignore[return-value]


def _price_adjustment(row: pd.Series, config: StrategyConfig) -> float:
    if not (
        config.production.enabled and config.production.use_raw_execution_prices
    ):
        return 0.0
    value = row.get("adjustment_points", 0.0)
    return 0.0 if pd.isna(value) else float(value)


def _in_decision_window(
    timestamp: object,
    *,
    start: object | None,
    end: object | None,
) -> bool:
    value = pd.Timestamp(timestamp)
    return not (
        (start is not None and value < pd.Timestamp(start))
        or (end is not None and value >= pd.Timestamp(end))
    )


def _signal_direction(signal: StrategySignal) -> str:
    if signal.target_position > 0:
        return "long"
    if signal.target_position < 0:
        return "short"
    return "flat"


def _account_equity(
    config: StrategyConfig,
    execution: SimulatedExecutionEngine,
    mark_price: float,
) -> float:
    """Convert point PnL into absolute account equity in currency units."""
    return float(config.sizing.capital) + (
        execution.mark_to_market(mark_price)
        * float(config.execution.contract_multiplier)
    )


def _executed_status(previous_position: int, fill: Fill) -> str:
    if fill.target_position == 0:
        return "executed_close"
    if previous_position == 0:
        return "executed_open"
    if previous_position * fill.target_position < 0:
        return "executed_reversal"
    return "executed_position_adjustment"


def _is_confirmed_opposite(intent, current_position: int) -> bool:
    if intent is None or intent.event is None or current_position == 0:
        return False
    if intent.event.state != SignalState.CONFIRMED:
        return False
    return (
        current_position > 0 and intent.event.direction == SignalDirection.SHORT
    ) or (
        current_position < 0 and intent.event.direction == SignalDirection.LONG
    )


# ════════════════════════════════════════════════════════════════
# 向后兼容 wrapper
# ════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ChanBacktestResult:
    """旧接口的简单返回类型（向后兼容）。"""
    bars: pd.DataFrame
    fills: pd.DataFrame
    summary: pd.DataFrame


@dataclass(frozen=True)
class ChanBacktestConfig:
    """旧版 ChanBacktestConfig（弃用）。

    请改用 StrategyConfig + run_backtest()。
    """
    code: str = "RB_MAIN"
    kl_type: KL_TYPE = KL_TYPE.K_1M
    fee_points: float = 1.0
    slippage_points: float = 1.0
    max_abs_position: int = 1
    max_loss_points: float | None = None
    allow_short: bool = True
    chan_config: dict | None = None


def run_chan_trigger_backtest(
    frame: pd.DataFrame,
    *,
    config: ChanBacktestConfig | None = None,
    limit: int | None = None,
) -> ChanBacktestResult:
    """旧版回测入口（弃用）。

    保持向后兼容。新代码请使用 run_backtest(StrategyConfig)。
    """
    warnings.warn(
        "run_chan_trigger_backtest() 已弃用，请迁移至 run_backtest(StrategyConfig)。",
        DeprecationWarning,
        stacklevel=2,
    )
    config = config or ChanBacktestConfig()

    strategy_config = StrategyConfig(
        code=config.code,
        kl_type=config.kl_type.name,
        allow_short=config.allow_short,
        execution=ExecutionParams(
            fee_points=config.fee_points,
            slippage_points=config.slippage_points,
        ),
        risk=RiskParams(
            max_abs_position=config.max_abs_position,
            max_loss_points=config.max_loss_points,
        ),
    )

    result = run_backtest(strategy_config, limit=limit, frame=frame)

    # Build old-style summary
    if result.bars.empty:
        max_dd = total_ret = 0.0
    else:
        curve = result.bars["equity_points"].astype(float)
        total_ret = float(curve.iloc[-1])
        max_dd = float((curve.cummax() - curve).max())

    summary = pd.DataFrame([{
        "code": config.code,
        "kl_type": config.kl_type.name,
        "bar_count": len(result.bars),
        "fill_count": len(result.fills),
        "total_return_points": total_ret,
        "max_drawdown_points": max_dd,
        "fee_points": config.fee_points,
        "slippage_points": config.slippage_points,
        "allow_short": config.allow_short,
    }])

    return ChanBacktestResult(
        bars=result.bars,
        fills=result.fills,
        summary=summary,
    )


def save_backtest_reports(result: ChanBacktestResult, output_dir: Path) -> None:
    """旧版保存接口（弃用）。"""
    warnings.warn(
        "save_backtest_reports() 已弃用，请使用 BacktestResult.save()。",
        DeprecationWarning,
        stacklevel=2,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result.bars.to_csv(output_dir / "chan_rb_minute_trend_bars.csv", index=False, encoding="utf-8-sig")
    result.fills.to_csv(output_dir / "chan_rb_minute_trend_fills.csv", index=False, encoding="utf-8-sig")
    result.summary.to_csv(output_dir / "chan_rb_minute_trend_summary.csv", index=False, encoding="utf-8-sig")
