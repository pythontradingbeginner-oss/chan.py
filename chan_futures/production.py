"""RB production data, out-of-sample walk-forward and paper replay tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from data_foundation.data.bars import aggregate_continuous_1m_to_Nm
from strategy_policy.reporting import compute_standard_metrics

from .config import StrategyConfig
from .feed import prepare_ohlc_frame


@dataclass(frozen=True, slots=True)
class RBProductionFrames:
    current: pd.DataFrame
    parent: pd.DataFrame
    child: pd.DataFrame


@dataclass(frozen=True, slots=True)
class ProductionFold:
    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def load_rb_production_frames(config: StrategyConfig) -> RBProductionFrames:
    """Aggregate one adjusted 1m source into time-aligned 5m/15m/60m frames."""
    source = pd.read_parquet(config.production.adjusted_1m_path)
    source = prepare_ohlc_frame(source)
    frames = {
        minutes: aggregate_continuous_1m_to_Nm(source, minutes)
        for minutes in (5, 15, 60)
    }
    current_minutes = _level_minutes(config.kl_type)
    parent_minutes = _level_minutes(config.multi_level.parent_kl_type)
    child_minutes = _level_minutes(config.multi_level.child_kl_type)
    if current_minutes not in frames:
        raise ValueError("P6 RB production replay supports 5m/15m/60m levels")
    return RBProductionFrames(
        current=frames[current_minutes],
        parent=frames[parent_minutes],
        child=frames[child_minutes],
    )


class ProductionWalkforwardRunner:
    """Run fixed-policy rolling OOS folds with point-in-time warmup only."""

    def __init__(
        self,
        config: StrategyConfig,
        *,
        train_months: int = 24,
        test_months: int = 3,
        step_months: int | None = None,
        max_folds: int | None = None,
    ) -> None:
        self.config = config
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months or test_months
        self.max_folds = max_folds
        if min(self.train_months, self.test_months, self.step_months) < 1:
            raise ValueError("walk-forward month windows must be positive")

    def folds(self, frame: pd.DataFrame) -> list[ProductionFold]:
        bars = prepare_ohlc_frame(frame)
        minimum = pd.Timestamp(bars["datetime"].min())
        maximum = pd.Timestamp(bars["datetime"].max())
        cursor = minimum
        result: list[ProductionFold] = []
        while True:
            train_end = cursor + pd.DateOffset(months=self.train_months)
            test_end = train_end + pd.DateOffset(months=self.test_months)
            if test_end > maximum:
                break
            result.append(
                ProductionFold(
                    fold=len(result) + 1,
                    train_start=cursor,
                    train_end=train_end,
                    test_start=train_end,
                    test_end=test_end,
                )
            )
            if self.max_folds is not None and len(result) >= self.max_folds:
                break
            cursor += pd.DateOffset(months=self.step_months)
        return result

    def run(
        self,
        *,
        frames: RBProductionFrames | None = None,
        output_dir: str | Path | None = None,
    ) -> pd.DataFrame:
        from .backtest import run_backtest

        frames = frames or load_rb_production_frames(self.config)
        rows: list[dict[str, object]] = []
        output = Path(output_dir) if output_dir is not None else None
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)

        for fold in self.folds(frames.current):
            test_mask = (
                (frames.current["datetime"] >= fold.test_start)
                & (frames.current["datetime"] < fold.test_end)
            )
            test_indices = frames.current.index[test_mask]
            if len(test_indices) == 0:
                continue
            warmup_start_idx = _replay_start_index(
                frames.current,
                int(test_indices[0]),
                self.config.production.warmup_bars,
            )
            replay_start = frames.current.iloc[warmup_start_idx]["datetime"]
            current = _slice(frames.current, replay_start, fold.test_end)
            parent = _slice(frames.parent, replay_start, fold.test_end)
            child = _slice(frames.child, replay_start, fold.test_end)
            result = run_backtest(
                self.config,
                frame=current,
                parent_frame=parent,
                child_frame=child,
                decision_start=fold.test_start,
                decision_end=fold.test_end,
            )
            oos_bars = result.bars[
                (result.bars["datetime"] >= fold.test_start)
                & (result.bars["datetime"] < fold.test_end)
            ].reset_index(drop=True)
            metrics = compute_standard_metrics(
                oos_bars,
                result.fills,
                result.trades,
                result.exit_events,
                strategy_name="rb_15m_qingpai_p6_oos",
                variant=f"fold_{fold.fold:02d}",
                timeframe_minutes=_level_minutes(self.config.kl_type),
            )
            row = {
                **asdict(fold),
                **metrics.to_dict(),
                "warmup_bars": int(test_indices[0]) - warmup_start_idx,
                "decision_count": len(result.decision_trace),
                "time_honest": all(
                    record.multi_level_time_honest is not False
                    for record in result.decision_trace
                ),
            }
            rows.append(row)
            if output is not None:
                result.save(output / f"fold_{fold.fold:02d}")

        summary = pd.DataFrame(rows)
        if output is not None:
            summary.to_csv(
                output / "walkforward_oos_summary.csv",
                index=False,
                encoding="utf-8-sig",
            )
        return summary


class PaperReplayRunner:
    """Replay the latest bars sequentially through the production decision core."""

    def __init__(self, config: StrategyConfig, *, paper_bars: int = 500) -> None:
        if paper_bars < 1:
            raise ValueError("paper_bars must be positive")
        self.config = config
        self.paper_bars = paper_bars

    def run(
        self,
        *,
        frames: RBProductionFrames | None = None,
        output_dir: str | Path | None = None,
    ):
        from .backtest import run_backtest

        frames = frames or load_rb_production_frames(self.config)
        decision_idx = max(0, len(frames.current) - self.paper_bars)
        replay_idx = _replay_start_index(
            frames.current,
            decision_idx,
            self.config.production.warmup_bars,
        )
        decision_start = frames.current.iloc[decision_idx]["datetime"]
        replay_start = frames.current.iloc[replay_idx]["datetime"]
        replay_end = frames.current.iloc[-1]["datetime"]
        result = run_backtest(
            self.config,
            frame=frames.current.iloc[replay_idx:].reset_index(drop=True),
            parent_frame=_slice(frames.parent, replay_start, replay_end, inclusive=True),
            child_frame=_slice(frames.child, replay_start, replay_end, inclusive=True),
            decision_start=decision_start,
        )
        if output_dir is not None:
            result.save(Path(output_dir))
        return result


def _slice(
    frame: pd.DataFrame,
    start: object,
    end: object,
    *,
    inclusive: bool = False,
) -> pd.DataFrame:
    upper = frame["datetime"] <= end if inclusive else frame["datetime"] < end
    return frame[(frame["datetime"] >= start) & upper].reset_index(drop=True)


def _level_minutes(level: str) -> int:
    values = {"K_5M": 5, "K_15M": 15, "K_30M": 30, "K_60M": 60}
    try:
        return values[level]
    except KeyError as exc:
        raise ValueError(f"unsupported production level: {level}") from exc


def _replay_start_index(
    frame: pd.DataFrame,
    decision_idx: int,
    minimum_warmup_bars: int,
) -> int:
    """Include at least the full active-contract history before a decision."""
    minimum_start = max(0, decision_idx - minimum_warmup_bars)
    if "active_symbol" not in frame.columns:
        return minimum_start
    symbol = str(frame.iloc[decision_idx]["active_symbol"])
    contract_rows = frame.index[
        frame["active_symbol"].astype(str) == symbol
    ]
    prior = [int(index) for index in contract_rows if int(index) <= decision_idx]
    contract_start = min(prior) if prior else decision_idx
    return min(minimum_start, contract_start)
