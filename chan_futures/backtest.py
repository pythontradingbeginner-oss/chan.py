from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE

from .execution import Fill, SimulatedExecutionEngine
from .feed import prepare_ohlc_frame, row_to_klu
from .risk import RiskConfig, RiskManager
from .strategy import MinimalChanTrendStrategy


@dataclass(frozen=True)
class ChanBacktestConfig:
    code: str = "RB_MAIN"
    kl_type: KL_TYPE = KL_TYPE.K_1M
    fee_points: float = 1.0
    slippage_points: float = 1.0
    max_abs_position: int = 1
    max_loss_points: float | None = None
    allow_short: bool = True
    chan_config: dict | None = None


@dataclass(frozen=True)
class ChanBacktestResult:
    bars: pd.DataFrame
    fills: pd.DataFrame
    summary: pd.DataFrame


def run_chan_trigger_backtest(
    frame: pd.DataFrame,
    *,
    config: ChanBacktestConfig | None = None,
    limit: int | None = None,
) -> ChanBacktestResult:
    """Replay futures minute bars through CChan.trigger_load and trade latest BSP."""
    config = config or ChanBacktestConfig()
    bars = prepare_ohlc_frame(frame)
    if limit is not None and limit > 0:
        bars = bars.iloc[:limit].reset_index(drop=True)

    chan = _new_empty_chan(config)
    strategy = MinimalChanTrendStrategy(allow_short=config.allow_short)
    risk = RiskManager(
        RiskConfig(
            max_abs_position=config.max_abs_position,
            max_loss_points=config.max_loss_points,
        )
    )
    execution = SimulatedExecutionEngine(
        fee_points=config.fee_points,
        slippage_points=config.slippage_points,
    )

    records: list[dict[str, object]] = []
    for row_number, row in bars.iterrows():
        klu = row_to_klu(row, kl_type=config.kl_type)
        chan.trigger_load({config.kl_type: [klu]})
        price = float(row["close"])
        timestamp = row["datetime"]
        active_symbol = row.get("active_symbol")
        signal = strategy.on_bar(
            chan=chan,
            current_position=execution.state.position,
            price=price,
            timestamp=timestamp,
            active_symbol=str(active_symbol) if pd.notna(active_symbol) else None,
        )
        fill: Fill | None = None
        risk_reason = None
        if signal is not None:
            decision = risk.approve(signal, realized_points=execution.state.realized_points)
            risk_reason = decision.reason
            if decision.approved:
                fill = execution.execute(signal)

        records.append(
            {
                "row_number": row_number,
                "datetime": timestamp,
                "close": price,
                "active_symbol": active_symbol,
                "position": execution.state.position,
                "avg_price": execution.state.avg_price,
                "realized_points": execution.state.realized_points,
                "equity_points": execution.mark_to_market(price),
                "signal_action": signal.action if signal else None,
                "signal_bsp_type": signal.bsp_type if signal else None,
                "risk_reason": risk_reason,
                "fill_price": fill.fill_price if fill else None,
            }
        )

    equity = pd.DataFrame(records)
    fills = pd.DataFrame([fill.__dict__ for fill in execution.fills])
    return ChanBacktestResult(
        bars=equity,
        fills=fills,
        summary=_build_summary(equity, fills, config),
    )


def save_backtest_reports(result: ChanBacktestResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    result.bars.to_csv(output_dir / "chan_rb_minute_trend_bars.csv", index=False, encoding="utf-8-sig")
    result.fills.to_csv(output_dir / "chan_rb_minute_trend_fills.csv", index=False, encoding="utf-8-sig")
    result.summary.to_csv(output_dir / "chan_rb_minute_trend_summary.csv", index=False, encoding="utf-8-sig")


def _new_empty_chan(config: ChanBacktestConfig) -> CChan:
    chan_config = {
        "trigger_step": True,
        "bi_strict": True,
        "divergence_rate": float("inf"),
        "bsp2_follow_1": False,
        "bsp3_follow_1": False,
        "min_zs_cnt": 0,
        "bs1_peak": False,
        "macd_algo": "peak",
        "bs_type": "1,1p,2,2s,3a,3b",
        "print_warning": False,
    }
    if config.chan_config:
        chan_config.update(config.chan_config)
    return CChan(
        code=config.code,
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[config.kl_type],
        config=CChanConfig(chan_config),
        autype=AUTYPE.NONE,
    )


def _build_summary(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    config: ChanBacktestConfig,
) -> pd.DataFrame:
    if equity.empty:
        max_drawdown = total_return = 0.0
    else:
        curve = equity["equity_points"].astype(float)
        total_return = float(curve.iloc[-1])
        max_drawdown = float((curve.cummax() - curve).max())
    return pd.DataFrame(
        [
            {
                "code": config.code,
                "kl_type": config.kl_type.name,
                "bar_count": int(len(equity)),
                "fill_count": int(len(fills)),
                "total_return_points": total_return,
                "max_drawdown_points": max_drawdown,
                "fee_points": config.fee_points,
                "slippage_points": config.slippage_points,
                "allow_short": config.allow_short,
            }
        ]
    )
