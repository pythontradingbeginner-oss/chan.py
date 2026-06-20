# Data Foundation

Reusable futures data preprocessing utilities extracted from
`price_action_structure_breakout`.

The first included data pipeline targets Shanghai Futures Exchange RB
one-minute bars imported by vn.py. It can clean multi-contract bars, build a
backward-adjusted continuous main-contract series, and aggregate complete
one-minute bars into higher timeframes.

## Data Preprocessing Pipeline

### Step 1: Clean Raw Data

```bash
C:\veighna_studio\python.exe scripts/run_data_cleaning.py --report
```

Outputs:

- `data/processed/RB_1m_raw_clean.parquet`: cleaned multi-contract one-minute data
- `data/processed/cleaning_log_YYYYMMDD.json`: cleaning audit counters
- `data/processed/quality_report_YYYYMMDD.html`: data quality report

### Step 2: Build Continuous Main Contract

```bash
C:\veighna_studio\python.exe scripts/run_build_continuous.py
```

Outputs:

- `data/processed/RB_1m_continuous.parquet`: backward-adjusted continuous one-minute data
- `data/processed/RB_main_switch_log.csv`: main-contract switch log

## Current RB Main Candidate

The current RB research candidate is fixed to:

- Entry: `bsp_1-1p-2_dc30_obv40_vol1_nostop`
- Exit: `trail_500_gb50`
- Frequency: 15-minute continuous main-contract bars
- Cost stress: at least `1` point fee plus `1` point slippage per transaction

Run the upgraded candidate only:

```bash
C:\veighna_studio\python.exe scripts/run_rb_main_candidate_strategy.py --reports-dir reports/rb_main_candidate_strategy --fee-points 1 --slippage-points 1
```

Run the exit-rule comparison around the same fixed entry:

```bash
C:\veighna_studio\python.exe scripts/run_rb_main_candidate_exit_research.py --reports-dir reports/rb_main_candidate_exit_research --fee-points 1 --slippage-points 1
```

### Step 3: Aggregate To 5m

```python
from data_foundation import load_continuous_1m, aggregate_continuous_1m_to_5m

df_1m = load_continuous_1m()
df_5m = aggregate_continuous_1m_to_5m(df_1m)
```

### Step 4: Detect DC Peaks And Valleys

Use `detect_dc_pivots` when you want a standalone event table of confirmed
Directional Change (DC) peaks and valleys:

```python
from data_foundation import detect_dc_pivots

dc_events = detect_dc_pivots(df_5m, threshold_points=20)
```

The returned event table contains one row per confirmed pivot:

- `dc_pivot_kind`: `peak` or `valley`
- `dc_pivot_price`: the close price at the pivot
- `dc_pivot_timestamp`: when the pivot actually occurred
- `dc_confirmation_price`: the later close price that confirmed the pivot
- `dc_confirmation_timestamp`: when the pivot became known
- `dc_confirmation_row`: the positional row number of that confirmation bar
- `dc_tmv`: `abs(current_pivot - previous_pivot) / threshold_points`
- `dc_elapsed_minutes`: minutes between the previous pivot and current pivot
- `dc_r`: `dc_tmv / dc_elapsed_minutes`

Use `add_dc_pivots` when you want those confirmed pivot fields written back to
the original bars:

```python
from data_foundation import add_dc_pivots

df_5m_with_pivots = add_dc_pivots(df_5m, threshold_points=20)
confirmed_pivots = df_5m_with_pivots[df_5m_with_pivots["dc_pivot_kind"].notna()]
```

Pivot fields are written only on the confirmation bar. Historical rows are not
backfilled, so the feature does not leak future pivot information.

### Step 5: Add DC Structure Features

```python
from data_foundation import add_dc_structure

df_5m_with_dc = add_dc_structure(
    df_5m,
    threshold_points=20,
    trend_buffer_points=15,
)
long_filter = df_5m_with_dc["dc_is_bull"]
short_filter = df_5m_with_dc["dc_is_bear"]
```

`add_dc_structure` builds on confirmed DC pivots and then classifies the market
structure as `bull`, `bear`, `sideways`, or `unknown`. A bull structure requires
the latest confirmed peak and latest confirmed valley to both rise by at least
`trend_buffer_points`; a bear structure requires both to fall by at least that
buffer.

### Step 6: Compare Close, Peak-Line, And Valley-Line MACD

```python
from data_foundation import add_dc_macd_comparison

df_5m_with_macd = add_dc_macd_comparison(df_5m)

bull_close_golden = df_5m_with_macd["dc_bull_close_macd_golden_cross"]
bear_peak_death = df_5m_with_macd["dc_bear_dc_peak_macd_death_cross"]
```

The peak and valley lines use only confirmed DC pivots and then forward-fill
from the confirmation bar, so historical rows are not backfilled with future
pivot information.

### Step 7: Add Trend-Bar MACD

```python
from data_foundation import add_trend_macd

df_5m_with_trend_macd = add_trend_macd(
    df_5m,
    threshold_type="atr_ratio",
    atr_k=1.0,
    atr_window=50,
)
```

The default trend-bar move is:

```python
trend_move = max(abs(close - open), abs(close - previous_close))
```

This keeps large candle bodies and gap-like close-to-close moves in the trend
bar set. The legacy body-only definition is still available with
`move_mode="body"`.

### Step 8: Plot DC Peaks, MACD, And OBV

```python
from data_foundation import load_replay_bars_jsonl, plot_dc_peak_valley_chart

bars = load_replay_bars_jsonl("reports/replay/.../rb_5m_bars.jsonl")
plot_dc_peak_valley_chart(
    bars,
    "reports/dc_chart.png",
    start="2018-03-22 21:00",
    end="2018-03-23 11:30",
    kline_minutes=5,
    source_minutes=5,
    dc_threshold_points=20,
    trend_buffer_points=15,
    obv_ma_period=30,
    min_width=14,
    width_per_bar=0.08,
    max_width=None,
)
```

Set `kline_minutes` to `10`, `30`, or another multiple of the source bar
period to plot higher-timeframe bars. The chart compresses market gaps by
default, so non-trading time does not leave blank space on the x-axis.
The chart width expands with the selected time range: `min_width` sets the
minimum figure width, `width_per_bar` controls how much width each plotted bar
adds, and `max_width` can cap very long charts when needed.

## DC Peak And Valley Definition

DC peak/valley detection is a threshold-based way to identify confirmed swing
turning points from close prices.

A `valley` is confirmed only after price rebounds at least `threshold_points`
from a candidate low. A `peak` is confirmed only after price falls at least
`threshold_points` from a candidate high.

For example, with `threshold_points=10`:

```text
100 -> 110 confirms 100 as a valley
110 -> 130 updates the candidate peak to 130
130 -> 121 is not enough to confirm the peak
130 -> 120 confirms 130 as a peak
```

The pivot occurred at the earlier extreme price, but it only becomes known on
the later confirmation bar:

- `dc_pivot_timestamp`: when the peak or valley actually occurred
- `dc_confirmation_timestamp`: when the later reversal confirmed it

The unfinished tail swing is intentionally ignored. If prices end with
`100 -> 110 -> 130 -> 120 -> 90 -> 99` and the threshold is `10`, the final
`90` is not a confirmed valley yet because price only rebounded to `99`, not
`100`. This avoids using future information.

For real-time strategy code, use the lower-level detector directly:

```python
from data_foundation import DirectionalChangeDetector

dc = DirectionalChangeDetector(threshold_points=20)

for bar in bars:
    event = dc.update(bar.close, bar.datetime)
    if event is None:
        continue

    if event.kind == "valley":
        handle_confirmed_valley(event)
    elif event.kind == "peak":
        handle_confirmed_peak(event)
```

`update` returns `None` while no new pivot is confirmed. It returns a `DCEvent`
when a confirmed pivot appears. `DCEvent.timestamp` is the pivot occurrence
time, while `DCEvent.confirmation_timestamp` is the bar where the reversal
threshold was reached.

## Public Imports

Projects can import from either the top-level package:

```python
from data_foundation import (
    DirectionalChangeDetector,
    add_dc_pivots,
    add_dc_macd_comparison,
    add_dc_structure,
    add_trend_macd,
    build_continuous_contract,
    clean_rb_1m_bars,
    detect_dc_pivots,
    load_replay_bars_jsonl,
    plot_dc_peak_valley_chart,
)
```

or the data/features subpackages:

```python
from data_foundation.charts import plot_dc_peak_valley_chart
from data_foundation.data import load_raw_from_vnpy
from data_foundation.features import add_dc_macd_comparison, add_dc_structure, add_trend_macd
from data_foundation.pivots import DirectionalChangeDetector, add_dc_pivots, detect_dc_pivots
```

## Included Scope

- Load RB bars from vn.py SQLite `DbBarData`
- Clean RB one-minute multi-contract bars
- Build backward-adjusted continuous main-contract data
- Aggregate continuous one-minute bars to complete N-minute bars
- Detect confirmed Directional Change peaks and valleys
- Add confirmed Directional Change pivot and bull/bear structure features
- Compare close, confirmed-peak-line, and confirmed-valley-line MACD signals
- Add trend-bar MACD with a default move based on
  `max(abs(close-open), abs(close-previous_close))`
- Plot DC peak/valley charts with MACD, OBV, and configurable OBV MA
- Generate lightweight quality reports

Strategy execution, backtesting modules, and HMM models are kept outside this
foundation package.

## chan.py Futures Bridge

This repository now includes a lightweight `chan_futures` package that keeps
trading code outside the core chan.py calculation modules:

- `chan_futures.feed`: converts futures OHLCV rows into `CKLine_Unit`
- `chan_futures.strategy`: minimal trend strategy driven by `get_latest_bsp()`
- `chan_futures.risk`: simple position and loss limits
- `chan_futures.execution`: point-based simulated fills and position state
- `chan_futures.backtest`: replays bars through `CChan.trigger_load()`

Run a smoke backtest on the included RB continuous one-minute data:

```bash
python scripts/run_chan_rb_minute_trend_strategy.py --limit 5000
```

Use `--limit 0` to replay the full selected range.
