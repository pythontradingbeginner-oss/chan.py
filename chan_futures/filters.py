"""入场信号过滤管线 —— DC 结构方向 + OBV + 成交量确认。

基于原始研究报告 (run_rb_chan_bsp_filter_param_search.py) 的相同过滤逻辑，
使用与原版一致的 sticky 趋势分类算法 (dc_mode="sticky", entry_buffer=15, exit_buffer=30)。

过滤维度:
  1. DC 结构方向
     - sticky 模式: 进入 bull/bear 只需单腿确认 (>=15点)
     - 退出 bull/bear 需要两个 legs 同时反转 (>=30点)
     - 做多仅在 bull 趋势中, 做空仅在 bear 趋势中
  2. OBV 均线方向
     - OBV > OBV_MA(window=40) → 做多有效
     - OBV < OBV_MA → 做空有效
  3. 成交量放大
     - volume > volume_ma(window=20) × strength → 有效
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from data_foundation.pivots import add_dc_pivots


# ════════════════════════════════════════════════════════════════
# Sticky DC 趋势分类 (与原版 run_dc_structure_candidate_diagnostics 一致)
# ════════════════════════════════════════════════════════════════

def _classify_pivot_structure(
    previous_peak: float,
    latest_peak: float,
    previous_valley: float,
    latest_valley: float,
    *,
    current_state: str,
    entry_buffer_points: float,
    exit_buffer_points: float,
) -> str:
    peak_delta = latest_peak - previous_peak
    valley_delta = latest_valley - previous_valley

    peaks_rising_entry = peak_delta >= entry_buffer_points
    valleys_rising_entry = valley_delta >= entry_buffer_points
    peaks_falling_entry = peak_delta <= -entry_buffer_points
    valleys_falling_entry = valley_delta <= -entry_buffer_points
    peaks_rising_exit = peak_delta >= exit_buffer_points
    valleys_rising_exit = valley_delta >= exit_buffer_points
    peaks_falling_exit = peak_delta <= -exit_buffer_points
    valleys_falling_exit = valley_delta <= -exit_buffer_points

    if current_state == "bull":
        return "sideways" if peaks_falling_exit and valleys_falling_exit else "bull"
    if current_state == "bear":
        return "sideways" if peaks_rising_exit and valleys_rising_exit else "bear"

    bull_leg = peaks_rising_entry or valleys_rising_entry
    bear_leg = peaks_falling_entry or valleys_falling_entry
    if bull_leg and not bear_leg:
        return "bull"
    if bear_leg and not bull_leg:
        return "bear"
    return "sideways"


def _trend_state_from_confirmed_pivots(
    frame: pd.DataFrame, *, entry_buffer_points: float, exit_buffer_points: float,
) -> pd.Series:
    states = np.full(len(frame), "unknown", dtype=object)
    peaks: list[dict[str, object]] = []
    valleys: list[dict[str, object]] = []
    current_state = "unknown"
    previous_row = 0

    def _write(start: int, stop: int) -> None:
        if start < stop:
            states[start:stop] = current_state

    pivot_rows = frame.index[frame["dc_pivot_kind"].notna()].to_list()
    for rn in pivot_rows:
        _write(previous_row, rn)
        kind = frame.at[rn, "dc_pivot_kind"]
        ev = {"price": float(frame.at[rn, "dc_pivot_price"])}
        if kind == "peak":
            peaks.append(ev)
        elif kind == "valley":
            valleys.append(ev)
        else:
            continue

        if len(peaks) >= 2 and len(valleys) >= 2:
            current_state = _classify_pivot_structure(
                float(peaks[-2]["price"]), float(peaks[-1]["price"]),
                float(valleys[-2]["price"]), float(valleys[-1]["price"]),
                current_state=current_state,
                entry_buffer_points=entry_buffer_points,
                exit_buffer_points=exit_buffer_points,
            )
        _write(rn, rn + 1)
        previous_row = rn + 1

    _write(previous_row, len(frame))
    return pd.Series(states, index=frame.index)


# ════════════════════════════════════════════════════════════════
# FilterPipeline
# ════════════════════════════════════════════════════════════════

@dataclass
class FilterContext:
    dc_is_bull: pd.Series
    dc_is_bear: pd.Series
    obv_above_ma: pd.Series
    obv_below_ma: pd.Series
    volume_amplified: pd.Series
    row_index_map: Callable[[object], int]


class FilterPipeline:
    """信号过滤管线: 预计算 → 逐根 bar 条件检查。"""

    def __init__(
        self,
        dc_threshold_points: float = 30.0,
        dc_entry_buffer_points: float = 15.0,
        dc_exit_buffer_points: float = 30.0,
        obv_window: int = 40,
        volume_window: int = 20,
        volume_strength: float = 1.0,
    ) -> None:
        self.dc_threshold_points = dc_threshold_points
        self.dc_entry_buffer_points = dc_entry_buffer_points
        self.dc_exit_buffer_points = dc_exit_buffer_points
        self.obv_window = obv_window
        self.volume_window = volume_window
        self.volume_strength = volume_strength

    def precompute(self, bars: pd.DataFrame) -> FilterContext:
        df = bars.reset_index(drop=True)

        # 1. DC pivots + sticky trend
        dc_df = add_dc_pivots(
            df, threshold_points=self.dc_threshold_points,
            datetime_col="datetime", price_col="close",
        )
        trend_labels = _trend_state_from_confirmed_pivots(
            dc_df, entry_buffer_points=self.dc_entry_buffer_points,
            exit_buffer_points=self.dc_exit_buffer_points,
        )

        # 2. OBV
        close = pd.to_numeric(df["close"], errors="coerce")
        volume = pd.to_numeric(df.get("volume", pd.Series(0.0, index=df.index)),
                               errors="coerce").fillna(0.0)
        direction = np.sign(close.diff()).fillna(0.0)
        obv = (direction * volume).cumsum()
        obv_ma = obv.rolling(self.obv_window, min_periods=self.obv_window).mean()

        # 3. Volume amplification
        volume_ma = volume.rolling(self.volume_window, min_periods=self.volume_window).mean()

        def _row_index(ts_or_row: object) -> int:
            if isinstance(ts_or_row, (int, np.integer)):
                return int(ts_or_row)
            if "datetime" in df.columns:
                ts = pd.Timestamp(ts_or_row) if not isinstance(ts_or_row, pd.Timestamp) else ts_or_row
                match = df.index[df["datetime"] == ts]
                if len(match) > 0:
                    return int(match[0])
            return -1

        return FilterContext(
            dc_is_bull=trend_labels == "bull",
            dc_is_bear=trend_labels == "bear",
            obv_above_ma=obv > obv_ma,
            obv_below_ma=obv < obv_ma,
            volume_amplified=volume > (volume_ma * self.volume_strength),
            row_index_map=_row_index,
        )

    def check_long(self, ctx: FilterContext, row_number: int) -> bool:
        """检查第 row_number 根 bar 是否满足做多条件。"""
        idx = _safe_iloc(ctx, row_number)
        if idx < 0 or idx >= len(ctx.dc_is_bull):
            return False
        return bool(
            ctx.dc_is_bull.iloc[idx]
            and ctx.obv_above_ma.iloc[idx]
            and ctx.volume_amplified.iloc[idx]
        )

    def check_short(self, ctx: FilterContext, row_number: int) -> bool:
        """检查第 row_number 根 bar 是否满足做空条件。"""
        idx = _safe_iloc(ctx, row_number)
        if idx < 0 or idx >= len(ctx.dc_is_bear):
            return False
        return bool(
            ctx.dc_is_bear.iloc[idx]
            and ctx.obv_below_ma.iloc[idx]
            and ctx.volume_amplified.iloc[idx]
        )


def _safe_iloc(ctx: FilterContext, row: int) -> int:
    try:
        return ctx.row_index_map(row)
    except Exception:
        return row
