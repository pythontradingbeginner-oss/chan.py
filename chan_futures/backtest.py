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

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
from .risk import RiskConfig, RiskManager
from .strategy import StrategySignal
from signal_core.models import SignalDecision, SignalDirection, SignalEvent
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


# ════════════════════════════════════════════════════════════════
# 主回测函数
# ════════════════════════════════════════════════════════════════


def run_backtest(
    config: StrategyConfig,
    *,
    limit: int | None = None,
    frame: pd.DataFrame | None = None,
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
        parent_frame=_load_level_frame(config.multi_level.parent_data_path)
        if config.multi_level.enabled
        else None,
        child_frame=_load_level_frame(config.multi_level.child_data_path)
        if config.multi_level.enabled
        else None,
    )

    # ── 出场规则 ──
    exit_manager = make_exit_manager(config)
    use_exit_rules = len(config.exits) > 0

    # ── 风控 + 执行 + 仓位 ──
    risk = RiskManager(RiskConfig(
        max_abs_position=config.risk.max_abs_position,
        max_loss_points=config.risk.max_loss_points,
    ))
    execution = SimulatedExecutionEngine(
        fee_points=config.execution.fee_points,
        slippage_points=config.execution.slippage_points,
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
]:
    """每根 K 线的主循环。"""

    use_filters = filter_ctx is not None and config.filter.enabled
    fee_points = config.execution.fee_points
    records: list[dict] = []
    exit_events: list[dict] = []
    trades: list[dict] = []
    position_context: PositionContext | None = None
    atr_values = _calculate_atr(bars, config.sizing.atr_period)
    _bar_events: list[SignalEvent] = []
    _event_ids: set[str] = set()
    _decisions: list[SignalDecision] = []
    extractor = decision_kernel.extractor

    def _close_trade(
        exit_bar: int, exit_price: float, exit_time,
        reason: str, rule_id: str = "strategy_reverse",
    ):
        nonlocal position_context
        if position_context is None:
            return
        entry_bar = position_context.entry_bar
        pnl = position_context.pnl_points(
            exit_price=exit_price,
            fee_points=fee_points,
        )
        trade = {
            "entry_bar": entry_bar,
            "entry_price": position_context.entry_price,
            "entry_time": position_context.entry_time,
            "direction": position_context.direction.value,
            "lots": position_context.volume,
            "grade": position_context.entry_grade,
            "active_symbol": position_context.active_symbol or "",
            "event_id": position_context.event_id,
            "signal_key": position_context.signal_key,
            "decision_id": position_context.decision_id,
            "setup_invalidation_price": position_context.setup_invalidation_price,
            "execution_stop_price": position_context.execution_stop_price,
            "exit_bar": exit_bar, "exit_price": exit_price,
            "exit_time": exit_time,
            "hold_bars": exit_bar - (entry_bar if entry_bar is not None else exit_bar),
            "pnl_points": round(pnl, 1),
            "exit_reason": reason, "exit_rule": rule_id,
        }
        trades.append(trade)
        # 通知风控系统
        risk.on_fill(
            pnl_points=round(pnl, 1),
            fill_time=exit_time,
            current_equity=execution.mark_to_market(exit_price),
        )
        position_context = None

    for row_number, row in bars.iterrows():
        klu = row_to_klu(row, kl_type=kl_type)
        chan.trigger_load({kl_type: [klu]})
        price = float(row["close"])
        timestamp = row["datetime"]
        if isinstance(timestamp, pd.Timestamp):
            dt_timestamp = timestamp.to_pydatetime()
        else:
            dt_timestamp = timestamp
        decomposition = decision_kernel.observe_structure(
            chan=chan,
            timestamp=timestamp,
            lv_idx=0,
        )
        active_symbol = row.get("active_symbol")
        active_symbol_str = str(active_symbol) if pd.notna(active_symbol) else None

        # ── 1. 检查出场规则 ──
        exit_signal: ExitSignal | None = None
        if use_exit_rules and exit_manager.is_active:
            exit_signal = exit_manager.check(
                bar_end_time=dt_timestamp,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=price,
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

        # ── 2. 检查入场信号 ──
        if exit_signal is None:
            evaluated = decision_kernel.evaluate_bar(
                chan=chan,
                current_position=execution.state.position,
                price=price, timestamp=timestamp,
                active_symbol=active_symbol_str,
                lv_idx=0,
                account_equity=(
                    config.sizing.capital
                    + execution.mark_to_market(price)
                    * config.execution.contract_multiplier
                ),
                atr=_finite_or_none(atr_values.iloc[row_number]),
            )
            if evaluated is not None:
                _decisions.append(evaluated.decision)
                if evaluated.event is not None and evaluated.event.event_id not in _event_ids:
                    _bar_events.append(evaluated.event)
                    _event_ids.add(evaluated.event.event_id)
            graded = evaluated if evaluated is not None and evaluated.accepted else None
            signal = graded.signal if graded is not None else None

            if signal is not None:
                # ── 0. 过滤器检查 (DC + OBV + Volume) ──
                if filter_ctx is not None and use_filters:
                    action = signal.action
                    # 仅在开仓/反转时检查过滤条件（平仓信号不拦截）
                    if action in ("open_long", "reverse_short_to_long"):
                        from .filters import FilterPipeline
                        pipeline = FilterPipeline()
                        if not pipeline.check_long(filter_ctx, row_number):
                            signal = None  # 过滤掉做多信号
                    elif action in ("open_short", "reverse_long_to_short"):
                        from .filters import FilterPipeline
                        pipeline = FilterPipeline()
                        if not pipeline.check_short(filter_ctx, row_number):
                            signal = None  # 过滤掉做空信号

                if signal is not None:
                    # 风控审批 + 执行
                    decision = risk.approve(
                        signal,
                        current_equity=execution.mark_to_market(price),
                    )
                    if decision.approved:
                        previous_position = execution.state.position
                        fill = execution.execute(signal)
                        closed_existing = previous_position != 0 and signal.action in (
                            "close_long", "close_short",
                            "reverse_long_to_short", "reverse_short_to_long",
                        )
                        if fill is not None and closed_existing:
                            _close_trade(
                                exit_bar=row_number, exit_price=fill.fill_price,
                                exit_time=timestamp, reason="strategy_reverse",
                                rule_id="strategy_reversal",
                            )
                            exit_manager.on_close()
                        if fill is not None and fill.target_position != 0:
                            position_context = graded.position_from_fill(
                                fill_price=fill.fill_price,
                                fill_volume=abs(fill.target_position),
                                fill_time=timestamp,
                                entry_bar=row_number,
                                active_symbol=active_symbol_str,
                            )
                            exit_manager.on_position_opened(position_context)

        # ── 3. 记录 bar-level 快照 ──
        records.append({
            "row_number": row_number, "datetime": timestamp,
            "close": price, "position": execution.state.position,
            "avg_price": execution.state.avg_price,
            "realized_points": execution.state.realized_points,
            "equity_points": execution.mark_to_market(price),
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
        exit_price = float(last_bar["close"])
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
