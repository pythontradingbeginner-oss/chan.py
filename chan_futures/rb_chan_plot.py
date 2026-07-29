from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE

from .feed import dataframe_to_klu_iter, prepare_ohlc_frame


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = REPO_ROOT / "data" / "processed" / "RB_15m_continuous_raw.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "rb_chan_plots"
DEFAULT_START = pd.Timestamp("2018-03-23 21:15:00")
DEFAULT_END = pd.Timestamp("2025-04-03 15:00:00")
DEFAULT_DISPLAY_BARS = 600
MIN_REQUIRED_BARS = 30
RB_15M_KL_TYPE = KL_TYPE.K_15M
LOCAL_TZ = "Asia/Shanghai"


DEFAULT_CHAN_CONFIG: dict[str, Any] = {
    "trigger_step": True,
    "bi_strict": True,
    "bs_type": "1,1p,2,2s,3a,3b",
    "zs_algo": "normal",
    "divergence_rate": float("inf"),
    "bsp2_follow_1": False,
    "bsp3_follow_1": False,
    "min_zs_cnt": 0,
    "bs1_peak": False,
    "macd_algo": "peak",
}


DEFAULT_PLOT_CONFIG: dict[str, bool] = {
    "plot_kline": True,
    "plot_kline_combine": True,
    "plot_bi": True,
    "plot_seg": True,
    "plot_eigen": False,
    "plot_zs": True,
    "plot_macd": False,
    "plot_mean": False,
    "plot_channel": False,
    "plot_bsp": True,
    "plot_extrainfo": False,
    "plot_demark": False,
    "plot_marker": False,
    "plot_rsi": False,
    "plot_kdj": False,
}


@dataclass(frozen=True)
class RbChanPlotResult:
    output_path: Path
    bar_count: int
    plotted_bar_count: int
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    bsp_count: int
    bi_count: int
    seg_count: int
    zs_count: int
    kl_type: KL_TYPE = RB_15M_KL_TYPE


def render_rb_chan_plot(
    *,
    data_path: str | Path = DEFAULT_DATA_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    filename: str | None = None,
    start: Any = DEFAULT_START,
    end: Any = DEFAULT_END,
    display_bars: int = DEFAULT_DISPLAY_BARS,
    overwrite: bool = False,
    min_bars: int = MIN_REQUIRED_BARS,
    frame: pd.DataFrame | None = None,
) -> RbChanPlotResult:
    """Replay RB 15m bars through CChan and save a chan.py structure PNG."""
    if display_bars < 0:
        raise ValueError("display_bars must be >= 0; use 0 to display all bars")

    source_frame = load_rb_15m_frame(data_path) if frame is None else frame
    bars = filter_rb_15m_frame(source_frame, start=start, end=end, min_bars=min_bars)

    actual_start = pd.Timestamp(bars.iloc[0]["datetime"])
    actual_end = pd.Timestamp(bars.iloc[-1]["datetime"])
    output_path = resolve_output_path(
        output_dir=output_dir,
        filename=filename or default_output_filename(actual_start, actual_end),
        overwrite=overwrite,
    )

    chan = build_rb_15m_chan(bars)
    save_chan_plot_image(
        chan=chan,
        output_path=output_path,
        display_bars=display_bars,
    )

    kl_data = chan[RB_15M_KL_TYPE]
    return RbChanPlotResult(
        output_path=output_path,
        bar_count=len(bars),
        plotted_bar_count=len(bars) if display_bars == 0 else min(len(bars), display_bars),
        start_time=actual_start,
        end_time=actual_end,
        bsp_count=len(kl_data.bs_point_lst),
        bi_count=len(kl_data.bi_list),
        seg_count=len(kl_data.seg_list),
        zs_count=len(kl_data.zs_list),
    )


def load_rb_15m_frame(data_path: str | Path = DEFAULT_DATA_PATH) -> pd.DataFrame:
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"RB 15m parquet not found: {path}")
    return pd.read_parquet(path)


def load_datetime_range(data_path: str | Path = DEFAULT_DATA_PATH) -> tuple[pd.Timestamp, pd.Timestamp]:
    frame = load_rb_15m_frame(data_path)
    prepared = prepare_rb_15m_frame(frame)
    if prepared.empty:
        raise ValueError("RB 15m parquet contains no rows")
    return pd.Timestamp(prepared.iloc[0]["datetime"]), pd.Timestamp(prepared.iloc[-1]["datetime"])


def prepare_rb_15m_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = prepare_ohlc_frame(frame)
    prepared["datetime"] = _to_local_naive_series(prepared["datetime"])
    prepared = prepared.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    return prepared


def filter_rb_15m_frame(
    frame: pd.DataFrame,
    *,
    start: Any = DEFAULT_START,
    end: Any = DEFAULT_END,
    min_bars: int = MIN_REQUIRED_BARS,
) -> pd.DataFrame:
    prepared = prepare_rb_15m_frame(frame)
    start_ts = _coerce_timestamp(start, "start")
    end_ts = _coerce_timestamp(end, "end")

    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start must be earlier than or equal to end")
    if start_ts is not None:
        prepared = prepared[prepared["datetime"] >= start_ts]
    if end_ts is not None:
        prepared = prepared[prepared["datetime"] <= end_ts]

    prepared = prepared.reset_index(drop=True)
    if prepared.empty:
        raise ValueError("该时间段没有 RB 15m 数据")
    if min_bars > 0 and len(prepared) < min_bars:
        raise ValueError(f"筛选后 K 线数量不足: {len(prepared)} < {min_bars}")
    return prepared


def build_rb_15m_chan(frame: pd.DataFrame) -> CChan:
    chan = CChan(
        code="RB",
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[RB_15M_KL_TYPE],
        config=make_rb_15m_chan_config(),
        autype=AUTYPE.NONE,
    )
    for klu in dataframe_to_klu_iter(frame, kl_type=RB_15M_KL_TYPE):
        chan.trigger_load({RB_15M_KL_TYPE: [klu]})
    return chan


def make_rb_15m_chan_config() -> CChanConfig:
    return CChanConfig(dict(DEFAULT_CHAN_CONFIG))


def save_chan_plot_image(
    *,
    chan: CChan,
    output_path: str | Path,
    display_bars: int = DEFAULT_DISPLAY_BARS,
) -> Path:
    if display_bars < 0:
        raise ValueError("display_bars must be >= 0; use 0 to display all bars")

    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from Plot.PlotDriver import CPlotDriver

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    plot_para = {
        "figure": {
            "x_range": int(display_bars),
            "x_tick_num": 10,
            "grid": "xy",
        }
    }
    driver = CPlotDriver(chan, plot_config=DEFAULT_PLOT_CONFIG, plot_para=plot_para)
    try:
        driver.save2img(str(output))
    finally:
        plt.close(driver.figure)
    return output


def resolve_output_path(
    *,
    output_dir: str | Path,
    filename: str,
    overwrite: bool = False,
) -> Path:
    name = ensure_png_filename(filename)
    output = Path(output_dir) / name
    if output.exists() and not overwrite:
        raise FileExistsError(f"output file already exists: {output}")
    return output


def ensure_png_filename(filename: str) -> str:
    name = filename.strip()
    if not name:
        raise ValueError("filename cannot be empty")
    return name if name.lower().endswith(".png") else f"{name}.png"


def default_output_filename(start: Any, end: Any) -> str:
    start_ts = _coerce_timestamp(start, "start")
    end_ts = _coerce_timestamp(end, "end")
    if start_ts is None or end_ts is None:
        raise ValueError("start and end are required for default filename")
    return f"RB_15m_chan_{_filename_time(start_ts)}_{_filename_time(end_ts)}.png"


def _filename_time(value: pd.Timestamp) -> str:
    return value.strftime("%Y%m%d_%H%M")


def _coerce_timestamp(value: Any, name: str) -> pd.Timestamp | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        ts = _to_local_naive_timestamp(value)
    except Exception as exc:  # noqa: BLE001 - keep UI-facing message clear
        raise ValueError(f"{name} is not a valid datetime: {value}") from exc
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts)


def _to_local_naive_series(series: pd.Series) -> pd.Series:
    return pd.Series(
        [_to_local_naive_timestamp(value) for value in series],
        index=series.index,
        name=series.name,
    )


def _to_local_naive_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(LOCAL_TZ).tz_localize(None)
    return ts
