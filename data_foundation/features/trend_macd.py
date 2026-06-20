from __future__ import annotations

import numpy as np
import pandas as pd


def add_trend_macd(
    frame: pd.DataFrame,
    *,
    threshold_type: str = "quantile",
    method: str = "freeze",
    move_mode: str = "body_or_close_to_previous_close",
    fixed_points: float | None = None,
    atr_k: float | None = None,
    atr_window: int | None = None,
    quantile_p: float | None = None,
    quantile_window: int | None = None,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    datetime_col: str = "datetime",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> pd.DataFrame:
    """Append trend-bar-filtered MACD columns using a freeze update rule.

    Dynamic thresholds are shifted by one bar so the current bar never
    participates in deciding whether it is a trend bar.

    By default, trend-bar strength is the larger of the candle body and the
    close-to-previous-close move, so gap-like moves are not ignored.
    """
    _validate_input_columns(
        frame,
        required=[datetime_col, open_col, high_col, low_col, close_col],
    )
    _validate_macd_periods(
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )
    if method != "freeze":
        raise ValueError("method must be 'freeze'")
    if move_mode not in {"body", "close_to_last_trend", "body_or_close_to_previous_close"}:
        raise ValueError(
            "move_mode must be one of: body, close_to_last_trend, "
            "body_or_close_to_previous_close"
        )

    result = frame.copy(deep=True)
    open_ = pd.to_numeric(result[open_col], errors="coerce")
    high = pd.to_numeric(result[high_col], errors="coerce")
    low = pd.to_numeric(result[low_col], errors="coerce")
    close = pd.to_numeric(result[close_col], errors="coerce")

    body = (close - open_).abs()
    threshold = _compute_threshold(
        threshold_type=threshold_type,
        body=body,
        high=high,
        low=low,
        close=close,
        fixed_points=fixed_points,
        atr_k=atr_k,
        atr_window=atr_window,
        quantile_p=quantile_p,
        quantile_window=quantile_window,
    )
    if move_mode == "body":
        trend_move = body
        reference_close = open_
        direction = np.sign(close - open_).fillna(0).astype(int)
        is_trend = threshold.notna() & trend_move.notna() & (trend_move >= threshold) & (direction != 0)
    elif move_mode == "body_or_close_to_previous_close":
        previous_close = close.shift(1)
        close_to_previous = (close - previous_close).abs()
        use_previous = close_to_previous.notna() & (close_to_previous > body)
        trend_move = body.astype("float64").where(~use_previous, close_to_previous)
        reference_close = open_.astype("float64").where(~use_previous, previous_close)
        direction = np.sign(close - reference_close).fillna(0).astype(int)
        is_trend = threshold.notna() & trend_move.notna() & (trend_move >= threshold) & (direction != 0)
    else:
        trend_move, reference_close, is_trend, direction = _compute_close_to_last_trend_move(
            close=close,
            threshold=threshold,
        )

    macd, signal, hist = _compute_freeze_macd(
        close=close,
        is_trend=is_trend,
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
    )

    result["trend_body"] = body
    result["trend_move"] = trend_move
    result["trend_reference_close"] = reference_close
    result["trend_move_mode"] = move_mode
    result["trend_threshold"] = threshold
    result["trend_is_bar"] = is_trend.fillna(False)
    result["trend_direction"] = direction.where(result["trend_is_bar"], 0)
    result["trend_macd"] = macd
    result["trend_macd_signal"] = signal
    result["trend_macd_hist"] = hist
    result["trend_macd_golden_cross"] = _crossed_above(macd, signal) & result["trend_is_bar"]
    result["trend_macd_death_cross"] = _crossed_below(macd, signal) & result["trend_is_bar"]
    return result


def _compute_close_to_last_trend_move(
    *,
    close: pd.Series,
    threshold: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    move_values: list[float] = []
    reference_values: list[float] = []
    trend_values: list[bool] = []
    direction_values: list[int] = []
    previous_close = np.nan
    last_trend_close = np.nan

    for price, threshold_value in zip(close, threshold):
        if pd.isna(price):
            move_values.append(np.nan)
            reference_values.append(np.nan)
            trend_values.append(False)
            direction_values.append(0)
            continue

        reference_close = last_trend_close if not pd.isna(last_trend_close) else previous_close
        if pd.isna(reference_close):
            trend_move = np.nan
            direction = 0
            is_trend = False
        else:
            trend_move = abs(float(price) - float(reference_close))
            raw_direction = np.sign(float(price) - float(reference_close))
            direction = int(raw_direction) if not pd.isna(raw_direction) else 0
            is_trend = (
                not pd.isna(threshold_value)
                and trend_move >= float(threshold_value)
                and direction != 0
            )

        if is_trend:
            last_trend_close = float(price)
        previous_close = float(price)

        move_values.append(trend_move)
        reference_values.append(reference_close)
        trend_values.append(is_trend)
        direction_values.append(direction)

    index = close.index
    return (
        pd.Series(move_values, index=index, dtype="float64"),
        pd.Series(reference_values, index=index, dtype="float64"),
        pd.Series(trend_values, index=index, dtype="bool"),
        pd.Series(direction_values, index=index, dtype="int64"),
    )


def _compute_threshold(
    *,
    threshold_type: str,
    body: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    fixed_points: float | None,
    atr_k: float | None,
    atr_window: int | None,
    quantile_p: float | None,
    quantile_window: int | None,
) -> pd.Series:
    if threshold_type == "fixed":
        if fixed_points is None or fixed_points <= 0:
            raise ValueError("fixed_points must be positive for fixed threshold")
        return pd.Series(float(fixed_points), index=body.index, dtype="float64")

    if threshold_type == "atr_ratio":
        if atr_k is None or atr_k <= 0:
            raise ValueError("atr_k must be positive for atr_ratio threshold")
        if atr_window is None or atr_window <= 0:
            raise ValueError("atr_window must be positive for atr_ratio threshold")
        true_range = _true_range(high=high, low=low, close=close)
        atr = true_range.rolling(atr_window, min_periods=atr_window).mean().shift(1)
        return atr * float(atr_k)

    if threshold_type == "quantile":
        if quantile_p is None or not 0 < quantile_p < 1:
            raise ValueError("quantile_p must be between 0 and 1 for quantile threshold")
        if quantile_window is None or quantile_window <= 0:
            raise ValueError("quantile_window must be positive for quantile threshold")
        return body.rolling(quantile_window, min_periods=quantile_window).quantile(
            float(quantile_p)
        ).shift(1)

    raise ValueError("threshold_type must be one of: fixed, atr_ratio, quantile")


def _true_range(*, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _compute_freeze_macd(
    *,
    close: pd.Series,
    is_trend: pd.Series,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_alpha = 2 / (fast_period + 1)
    slow_alpha = 2 / (slow_period + 1)
    signal_alpha = 2 / (signal_period + 1)

    macd_values: list[float] = []
    signal_values: list[float] = []
    hist_values: list[float] = []
    fast_ema = np.nan
    slow_ema = np.nan
    macd_signal = 0.0

    for price, trend in zip(close, is_trend):
        if pd.isna(price):
            macd_values.append(np.nan)
            signal_values.append(np.nan)
            hist_values.append(np.nan)
            continue

        if pd.isna(fast_ema):
            fast_ema = float(price)
            slow_ema = float(price)

        if bool(trend):
            fast_ema = float(price) * fast_alpha + fast_ema * (1 - fast_alpha)
            slow_ema = float(price) * slow_alpha + slow_ema * (1 - slow_alpha)
            current_macd = fast_ema - slow_ema
            macd_signal = current_macd * signal_alpha + macd_signal * (1 - signal_alpha)
        else:
            current_macd = fast_ema - slow_ema

        macd_values.append(current_macd)
        signal_values.append(macd_signal)
        hist_values.append(current_macd - macd_signal)

    index = close.index
    return (
        pd.Series(macd_values, index=index, dtype="float64"),
        pd.Series(signal_values, index=index, dtype="float64"),
        pd.Series(hist_values, index=index, dtype="float64"),
    )


def _crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    current = left > right
    previous = left.shift(1) <= right.shift(1)
    valid = left.notna() & right.notna() & left.shift(1).notna() & right.shift(1).notna()
    return (current & previous & valid).fillna(False)


def _crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    current = left < right
    previous = left.shift(1) >= right.shift(1)
    valid = left.notna() & right.notna() & left.shift(1).notna() & right.shift(1).notna()
    return (current & previous & valid).fillna(False)


def _validate_input_columns(frame: pd.DataFrame, *, required: list[str]) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required column(s): {', '.join(missing)}")


def _validate_macd_periods(
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> None:
    if fast_period <= 0:
        raise ValueError("fast_period must be positive")
    if slow_period <= 0:
        raise ValueError("slow_period must be positive")
    if signal_period <= 0:
        raise ValueError("signal_period must be positive")
    if fast_period >= slow_period:
        raise ValueError("fast_period must be smaller than slow_period")
