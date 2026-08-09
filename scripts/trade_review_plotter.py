"""生成交易复盘图表 V2 —— 严格重放 holdout 窗口，真实成交价与真实策略配置。

设计原则（对应 Codex 12 条整改）：
  1. 坐标诚实：entry_x/exit_x 直接使用 replay 内 KLU 索引（CChan 的 klu.idx
     恰好等于 replay 行号），保存前断言该索引对应 datetime 与 holdout 时间完全相等；
     入场/出场竖线同时画到价格子图和 MACD 子图。
  2. 严格重放：复用 scripts/run_p7_holdout.py 的窗口逻辑 —— 加载 15m/60m/5m
     三级别数据，warmup = max(2000, configured_warmup_bars)，decision_start=
     holdout 记录的 oos_start。scenario 0/1/2 应用 holdout 记录的 fee/slippage 倍率。
     绝不跑 2018-2026 全量历史后再宽松匹配。
  3. 禁止静默推断：默认回放/交易匹配失败即终止；仅当用户显式传
     --allow-inferred-prices 时才允许 close 推断，并在 PNG/CSV/HTML 打红色
     INFERRED / NOT ACTUAL FILL 水印。
  4. 真实 fill：从 backtest fills 取 signal_price / fill_price / 双边滑点 / 手续费，
     图中主要水平线使用 fill_price，并清楚标注信号价与成交价；校验每笔
     gross - fees - slippage = net。
  5. 真实策略 Chan 配置：用 config.chan.to_dict() 构造 CChan（macd_algo=full_area），
     绝不用 rb_chan_plot.py 的默认 peak 配置。
  6. 时间诚实：分别构造 entry/exit/context 三个独立 CChan 截止状态。entry
     快照只喂到入场 K 线，exit 快照只喂到出场 K 线；出场后 post_bars 只出现在
     HINDSIGHT CONTEXT 面板，绝不用于前两个面板的结构计算。
  7. decision_trace：写入 BSP/grade/event_id/signal_key/decision_id/policy_id、
     父级方向、子级确认、动量指标、失效位、初始止损、exit_rule、exit trigger
     price 与可见时间。
  8. 视觉：原生 Chan BSP（灰菱形）与真实交易标记（TRADE ENTRY/EXIT）颜色+图例区分；
     标题放 figure suptitle；入场价/止损线只画在实际持仓区间。
  9. manifest：scenario、全局 trade_index（原序号）、price_source、signal/fill price、
     config/data SHA256、报告 git_head、当前 git_head、marker datetime、全部
     decision_trace 字段与校验状态；筛选后保留原始交易序号。
  10. UI：scenario 带名称下拉框；出场原因从 JSON 动态读取；交易多选表；后台线程
      执行并支持取消；每次运行独立输出子目录；HTML 只展示成功且来源校验通过的图。
  11/12. 测试与验收见 tests/test_trade_review_plotter.py 与任务要求。

用法（CLI）:
    python scripts/trade_review_plotter.py --no-ui --scenario 0
    python scripts/trade_review_plotter.py --no-ui --scenario 0 --pnl-filter winning
    python scripts/trade_review_plotter.py --no-ui --scenario 1 --output-dir reports/trade_review_v2
    python scripts/trade_review_plotter.py --no-ui --allow-inferred-prices   # 不推荐

GUI:
    python scripts/trade_review_plotter.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GIT = os.environ.get("GIT_EXECUTABLE") or shutil.which("git") or "git"


# ── 默认路径 ────────────────────────────────────────────────────────────────
DEFAULT_HOLDOUT = (
    Path(os.environ.get("TEMP", tempfile.gettempdir()))
    / "p7_holdout"
    / "holdout_r10_20260806_032914.json"
)
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "rb_15m_qingpai_strict.yaml"
DEFAULT_DATA_15M = PROJECT_ROOT / "data" / "processed" / "RB_15m_continuous_raw.parquet"
DEFAULT_DATA_60M = PROJECT_ROOT / "data" / "processed" / "RB_60m_continuous_raw.parquet"
DEFAULT_DATA_5M = PROJECT_ROOT / "data" / "processed" / "RB_5m_continuous_raw.parquet"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "trade_review_v2"
DEFAULT_OOS_START = "2026-06-01"
DEFAULT_WARMUP_BARS = 2000
DEFAULT_PRE_BARS = 120
DEFAULT_POST_BARS = 60

# scenario 名称表（顺序与 run_p7_holdout.py 的 scenarios 一致）
SCENARIO_META = [
    ("baseline (cost 1.0x)", 1.0, 1.0),
    ("cost 1.5x", 1.5, 1.5),
    ("cost 2.0x", 2.0, 2.0),
]

# 校验阈值（点）
_COST_EPS = 0.01

# 水印文本（允许推断时）
_WATERMARK = "INFERRED / NOT ACTUAL FILL"


# ── matplotlib CJK 字体（Windows）───────────────────────────────────────────
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # Microsoft YaHei
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",    # SimHei
    r"C:\Windows\Fonts\simsun.ttc",    # SimSun
    r"C:\Windows\Fonts\Deng.ttf",      # DengXian
]


def _setup_cjk_font() -> str | None:
    """注册一个可用的 CJK 字体并写入 rcParams；返回字体名或 None。"""
    try:
        from matplotlib import font_manager, rcParams
        from matplotlib.ft2font import FT2Font
    except Exception:
        return None
    for candidate in _CJK_FONT_CANDIDATES:
        if os.path.exists(candidate):
            try:
                FT2Font(candidate)  # 校验字体可读
                font_manager.fontManager.addfont(candidate)
                name = font_manager.FontProperties(fname=candidate).get_name()
                rcParams["font.family"] = "sans-serif"
                rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
                rcParams["axes.unicode_minus"] = False
                return name
            except Exception:
                continue
    return None


def _apply_cjk_font_before_plot() -> None:
    """在任何 matplotlib 绘图前调用。"""
    _setup_cjk_font()


class CancelledError(RuntimeError):
    """用户点击取消时抛出。"""


class TradeReviewError(RuntimeError):
    """回放/匹配失败时抛出（fail-closed）。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 基础工具
# ═══════════════════════════════════════════════════════════════════════════════


def _naive_ts(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def _inclusive_end_ts(value: Any) -> pd.Timestamp:
    """Treat a date-only upper bound as the end of that calendar day."""
    ts = _naive_ts(value)
    if isinstance(value, str) and len(value.strip()) <= 10:
        return ts + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return ts


def _normalize_datetime(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    dt = pd.to_datetime(f["datetime"])
    if dt.dt.tz is not None:
        dt = dt.dt.tz_localize(None)
    f["datetime"] = dt
    f = f.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    return f


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _git_output(*args: str) -> str:
    try:
        out = subprocess.run(
            [GIT, "-C", str(PROJECT_ROOT), *args],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _abs(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _sanitize(name: str) -> str:
    for ch in "/\\:()[] ":
        name = name.replace(ch, "_")
    return name.strip("_")


def _safe_float(value: Any) -> float:
    try:
        f = float(value)
        if pd.isna(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# ═══════════════════════════════════════════════════════════════════════════════
# holdout 读取 + scenario
# ═══════════════════════════════════════════════════════════════════════════════


def load_holdout(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"holdout JSON 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def scenario_name(index: int) -> str:
    if 0 <= index < len(SCENARIO_META):
        return SCENARIO_META[index][0]
    return f"scenario{index}"


def select_scenario(data: dict, scenario_index: int = 0) -> tuple[list[dict], dict]:
    """取出指定 scenario 的交易列表 + scenario 元信息。"""
    scenarios = data.get("scenarios", [])
    if not scenarios:
        raise TradeReviewError("holdout JSON 中没有 scenarios")
    if scenario_index < 0 or scenario_index >= len(scenarios):
        raise TradeReviewError(
            f"scenario_index={scenario_index} 超出范围 (共 {len(scenarios)} 个)"
        )
    meta = dict(scenarios[scenario_index])
    return list(meta.get("trades", [])), meta


def assign_original_index(trades: list[dict]) -> list[dict]:
    """给每笔交易打上稳定的全局交易序号（1..N），筛选后保留。"""
    out = []
    for i, trade in enumerate(trades):
        t = dict(trade)
        t["_orig_index"] = i + 1
        out.append(t)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 窗口逻辑（复用 run_p7_holdout 的窗口计算）
# ═══════════════════════════════════════════════════════════════════════════════


def build_replay_window(
    frame_15m: pd.DataFrame,
    *,
    oos_start: Any = DEFAULT_OOS_START,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    configured_warmup_bars: int | None = None,
) -> pd.DataFrame:
    """按 holdout 规则切片 15m frame：warmup=max(2000, configured) + OOS。"""
    cur = frame_15m.reset_index(drop=True)
    oos_ts = _naive_ts(oos_start)
    warm = cur[cur["datetime"] < oos_ts]
    cap = max(int(warmup_bars), int(configured_warmup_bars or 0))
    warm_idx = max(0, len(warm) - cap)
    replay = cur.iloc[warm_idx:].reset_index(drop=True)
    if replay.empty:
        raise TradeReviewError("回放窗口为空")
    return replay


def _slice_from(frame: pd.DataFrame, start_ts: Any) -> pd.DataFrame:
    f = frame.copy()
    f["datetime"] = pd.to_datetime(f["datetime"])
    if f["datetime"].dt.tz is not None:
        f["datetime"] = f["datetime"].dt.tz_localize(None)
    return f[f["datetime"] >= _naive_ts(start_ts)].reset_index(drop=True)


def find_bar_index(frame: pd.DataFrame, timestamp: Any, what: str) -> int:
    """在 replay frame 中定位精确 datetime；找不到即失败（fail-closed）。"""
    ts = _naive_ts(timestamp)
    mask = frame["datetime"] == ts
    hits = frame.index[mask]
    if len(hits) == 0:
        raise TradeReviewError(
            f"{what} 时间 {ts} 在回放窗口 {frame['datetime'].iloc[0]} ~ "
            f"{frame['datetime'].iloc[-1]} 中不存在"
        )
    return int(hits[0])


def compute_trade_window(
    replay: pd.DataFrame,
    entry_time: Any,
    exit_time: Any,
    *,
    pre_bars: int = DEFAULT_PRE_BARS,
    post_bars: int = DEFAULT_POST_BARS,
) -> dict:
    """计算 entry/exit 的 replay 行索引与展示窗口起止。"""
    entry_idx = find_bar_index(replay, entry_time, "entry")
    exit_idx = find_bar_index(replay, exit_time, "exit")
    if exit_idx < entry_idx:
        raise TradeReviewError("exit 时间早于 entry 时间")
    plot_start = max(0, entry_idx - pre_bars)
    plot_end = min(len(replay) - 1, exit_idx + post_bars)
    return {
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "plot_start": plot_start,
        "plot_end": plot_end,
        "display_bars": plot_end - plot_start + 1,
        "future_bars": plot_end - exit_idx,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 回测重放（严格窗口 + scenario 成本）
# ═══════════════════════════════════════════════════════════════════════════════


def apply_scenario_execution(config, fee_mult: float, slip_mult: float):
    """按 scenario 倍率复制 execution 配置（保持其余字段不变）。"""
    exec_cfg = config.execution.__class__(
        fee_points=config.execution.fee_points * fee_mult,
        slippage_points=config.execution.slippage_points * slip_mult,
        contract_multiplier=config.execution.contract_multiplier,
        price_tick=config.execution.price_tick,
        margin_rate=config.execution.margin_rate,
        max_margin_utilization=config.execution.max_margin_utilization,
    )
    return config.__class__(
        code=config.code,
        kl_type=config.kl_type,
        chan=config.chan,
        grading=config.grading,
        decomposition=config.decomposition,
        multi_level=config.multi_level,
        momentum=config.momentum,
        production=config.production,
        entry=config.entry,
        exits=config.exits,
        risk=config.risk,
        sizing=config.sizing,
        execution=exec_cfg,
    )


def run_replay_backtest(
    config,
    *,
    frame_15m: pd.DataFrame,
    frame_60m: pd.DataFrame,
    frame_5m: pd.DataFrame,
    oos_start: Any,
    warmup_bars: int,
    fee_mult: float,
    slip_mult: float,
):
    """按 scenario 倍率复制 execution config，在窗口内跑一次回测。"""
    replay = build_replay_window(
        frame_15m,
        oos_start=oos_start,
        warmup_bars=warmup_bars,
        configured_warmup_bars=config.production.warmup_bars,
    )
    parent = _slice_from(frame_60m, replay["datetime"].iloc[0])
    child = _slice_from(frame_5m, replay["datetime"].iloc[0])

    cfg2 = apply_scenario_execution(config, fee_mult, slip_mult)
    from chan_futures.backtest import run_backtest

    result = run_backtest(
        cfg2,
        frame=replay,
        parent_frame=parent,
        child_frame=child,
        decision_start=_naive_ts(oos_start),
    )
    return replay, result


def _trade_match_key(trade: dict) -> tuple:
    return (
        str(trade.get("entry_time", ""))[:19],
        str(trade.get("exit_time", ""))[:19],
        str(trade.get("direction", "")),
        str(trade.get("exit_reason", "")),
    )


def _build_backtest_index(result) -> dict[tuple, dict]:
    index: dict[tuple, dict] = {}
    for t in result.trades:
        key = _trade_match_key(t)
        if key in index:
            raise TradeReviewError(f"回测交易键不唯一，无法精确匹配: {key}")
        index[key] = t
    return index


def _backtest_audit_pnl(
    holdout_trade: dict,
    bt_trade: dict,
    fills: dict,
) -> tuple[bool, str]:
    """Cross-check holdout arithmetic against the fresh replay and its fills."""
    gross = _safe_float(holdout_trade.get("gross_pnl_points"))
    fees = _safe_float(holdout_trade.get("fees_points"))
    slip = _safe_float(holdout_trade.get("slippage_points"))
    net = _safe_float(holdout_trade.get("net_pnl_points"))
    replay_net = _safe_float(bt_trade.get("pnl_points"))
    replay_fees = _safe_float(fills.get("entry_fee_points")) + _safe_float(
        fills.get("exit_fee_points")
    )
    replay_slip = _safe_float(fills.get("entry_slippage_points")) + _safe_float(
        fills.get("exit_slippage_points")
    )
    checks = {
        "holdout_equation": abs(gross - fees - slip - net) < _COST_EPS,
        "replay_net": abs(replay_net - net) < _COST_EPS,
        "replay_fees": abs(replay_fees - fees) < _COST_EPS,
        "replay_slippage": abs(replay_slip - slip) < _COST_EPS,
    }
    ok = all(checks.values())
    failed = ",".join(name for name, passed in checks.items() if not passed) or "none"
    detail = (
        f"holdout(gross={gross:.1f},fees={fees:.1f},slip={slip:.1f},net={net:.1f}); "
        f"replay(net={replay_net:.1f},fees={replay_fees:.1f},slip={replay_slip:.1f}); "
        f"failed={failed}"
    )
    return ok, detail


def _lookup_fills(result, entry_time: Any, exit_time: Any) -> dict:
    """从 fills 中取 entry/exit 的 signal_price / fill_price / 滑点 / 费用。"""
    fills = result.fills
    entry_ts = _naive_ts(entry_time)
    exit_ts = _naive_ts(exit_time)
    out = {
        "entry_signal_price": None,
        "entry_fill_price": None,
        "entry_slippage_points": None,
        "entry_fee_points": None,
        "exit_signal_price": None,
        "exit_fill_price": None,
        "exit_slippage_points": None,
        "exit_fee_points": None,
    }
    if fills is None or len(fills) == 0:
        return out
    fts = pd.to_datetime(fills["timestamp"])
    if fts.dt.tz is not None:
        fts = fts.dt.tz_localize(None)

    entry_rows = fills[(fts == entry_ts) & (fills["target_position"].astype(int) != 0)]
    if len(entry_rows) > 1:
        raise TradeReviewError(f"入场时点存在多条候选 fill，无法唯一匹配: {entry_ts}")
    if len(entry_rows) > 0:
        r = entry_rows.iloc[0]
        out["entry_signal_price"] = _safe_float(r["signal_price"])
        out["entry_fill_price"] = _safe_float(r["fill_price"])
        out["entry_slippage_points"] = abs(
            _safe_float(r["fill_price"]) - _safe_float(r["signal_price"])
        )
        out["entry_fee_points"] = _safe_float(result.config.execution.fee_points) * abs(
            int(r["quantity_delta"])
        )

    exit_rows = fills[(fts == exit_ts) & (fills["target_position"].astype(int) == 0)]
    if len(exit_rows) > 1:
        raise TradeReviewError(f"出场时点存在多条候选 fill，无法唯一匹配: {exit_ts}")
    if len(exit_rows) > 0:
        r = exit_rows.iloc[0]
        out["exit_signal_price"] = _safe_float(r["signal_price"])
        out["exit_fill_price"] = _safe_float(r["fill_price"])
        out["exit_slippage_points"] = abs(
            _safe_float(r["fill_price"]) - _safe_float(r["signal_price"])
        )
        out["exit_fee_points"] = _safe_float(result.config.execution.fee_points) * abs(
            int(r["quantity_delta"])
        )
    return out


def _lookup_exit_event(result, exit_time: Any) -> dict:
    """从 exit_events 取 exit_rule / trigger price / fill price。"""
    ev = result.exit_events
    if ev is None or len(ev) == 0:
        return {}
    ets = pd.to_datetime(ev["datetime"])
    if ets.dt.tz is not None:
        ets = ets.dt.tz_localize(None)
    rows = ev[ets == _naive_ts(exit_time)]
    if len(rows) == 0:
        return {}
    r = rows.iloc[0]
    return {
        "exit_rule": _safe_str(r.get("rule_id")),
        "exit_trigger_price": _safe_float(r.get("trigger_price")),
        "exit_event_fill_price": _safe_float(r.get("fill_price")),
    }


def _lookup_decision_trace(
    result,
    entry_time: Any,
    direction: str,
    *,
    event_id: str = "",
    signal_key: str = "",
) -> dict:
    """Return the unique accepted trace matching time, direction and identity."""
    ts = _naive_ts(entry_time)
    matched = []
    for rec in result.decision_trace:
        rec_ts = _naive_ts(rec.timestamp)
        if rec_ts != ts:
            continue
        if not rec.accepted:
            continue
        if rec.direction != direction:
            continue
        item = rec.to_dict()
        if event_id and _safe_str(item.get("event_id")) != event_id:
            continue
        if signal_key and _safe_str(item.get("signal_key")) != signal_key:
            continue
        matched.append(item)
    if len(matched) != 1:
        raise TradeReviewError(
            "入场 decision_trace 必须唯一匹配: "
            f"time={ts}, direction={direction}, event_id={event_id}, "
            f"signal_key={signal_key}, matches={len(matched)}"
        )
    return matched[0]


def enrich_trades(
    holdout_trades: list[dict],
    result,
    *,
    fee_points: float,
    slippage_points: float,
    allow_inferred: bool,
    replay: pd.DataFrame,
    require_exact_trade_set: bool = True,
) -> list[dict]:
    """把 backtest 真实 fill / trace / 校验状态合并到 holdout 交易上。

    默认 fail-closed：任何一笔匹配失败即抛 TradeReviewError。
    仅当 allow_inferred=True 时才退化到 K 线 close 推断。
    """
    bt_index = _build_backtest_index(result)
    bt_trades = list(result.trades)
    expected_fee = _safe_float(result.config.execution.fee_points)
    expected_slip = _safe_float(result.config.execution.slippage_points)
    if abs(expected_fee - fee_points) >= _COST_EPS:
        raise TradeReviewError(
            f"回放手续费配置不一致: result={expected_fee}, expected={fee_points}"
        )
    if abs(expected_slip - slippage_points) >= _COST_EPS:
        raise TradeReviewError(
            f"回放滑点配置不一致: result={expected_slip}, expected={slippage_points}"
        )

    holdout_keys = [_trade_match_key(t) for t in holdout_trades]
    if len(set(holdout_keys)) != len(holdout_keys):
        raise TradeReviewError("holdout 交易键不唯一，无法逐笔精确匹配")
    if require_exact_trade_set and not allow_inferred:
        bt_keys = set(bt_index)
        source_keys = set(holdout_keys)
        if source_keys != bt_keys or len(holdout_trades) != len(bt_trades):
            missing = sorted(source_keys - bt_keys)
            extra = sorted(bt_keys - source_keys)
            raise TradeReviewError(
                "回放与 holdout 交易集合不完全一致: "
                f"holdout={len(holdout_trades)}, replay={len(bt_trades)}, "
                f"missing={missing}, extra={extra}"
            )

    unmatched: list[dict] = []
    for trade in holdout_trades:
        key = _trade_match_key(trade)
        if key not in bt_index:
            unmatched.append(trade)

    if unmatched:
        if allow_inferred:
            print(
                f"[警告] {len(unmatched)} 笔交易未与回测精确匹配，改用 close 推断"
                "（仅因显式开启 --allow-inferred-prices）",
                file=sys.stderr,
            )
        else:
            lines = "\n".join(
                f"  {t.get('entry_time')} -> {t.get('exit_time')} "
                f"{t.get('direction')} {t.get('exit_reason')}"
                for t in unmatched
            )
            raise TradeReviewError(
                f"回放与 holdout 匹配失败（{len(unmatched)} 笔未匹配，"
                "默认禁止静默推断；可显式加 --allow-inferred-prices）：\n"
                f"{lines}\n回测共 {len(bt_trades)} 笔。"
            )

    enriched: list[dict] = []
    for trade in holdout_trades:
        key = _trade_match_key(trade)
        bt = bt_index.get(key)

        # 未匹配：仅 allow_inferred 时退化为 close 推断
        if bt is None:
            if not allow_inferred:
                raise TradeReviewError(
                    f"交易 {trade.get('entry_time')} -> {trade.get('exit_time')} "
                    "未与回测精确匹配（默认禁止静默推断）"
                )
            out = _infer_from_close(dict(trade), replay)
            out["price_source"] = "close_inferred"
            out["entry_signal_price"] = out["entry_price"]
            out["entry_fill_price"] = out["entry_price"]
            out["exit_signal_price"] = out["exit_price"]
            out["exit_fill_price"] = out["exit_price"]
            out["entry_slippage_points"] = None
            out["exit_slippage_points"] = None
            out["computed_fees_points"] = None
            out["computed_slippage_points"] = None
            out["grade"] = ""
            out["event_id"] = ""
            out["signal_key"] = ""
            out["decision_id"] = ""
            out["policy_id"] = ""
            out["bsp_type"] = ""
            out["setup_invalidation_price"] = None
            out["execution_stop_price"] = None
            out["exit_rule"] = ""
            out["exit_trigger_price"] = None
            out["backtest_pnl_points"] = None
            out["cost_check"] = "N/A"
            out["cost_check_detail"] = "price inferred from close"
            out["decision_trace"] = {}
            out["replay_match_check"] = "inferred"
            out["decision_trace_check"] = "N/A"
            enriched.append(out)
            continue

        out = dict(trade)

        fills = _lookup_fills(result, trade.get("entry_time"), trade.get("exit_time"))
        exit_ev = _lookup_exit_event(result, trade.get("exit_time"))
        trace = _lookup_decision_trace(
            result,
            trade.get("entry_time"),
            str(trade.get("direction")),
            event_id=_safe_str(bt.get("event_id")),
            signal_key=_safe_str(bt.get("signal_key")),
        )

        out["entry_price"] = fills.get("entry_fill_price")
        out["exit_price"] = fills.get("exit_fill_price")
        out["entry_signal_price"] = fills.get("entry_signal_price")
        out["entry_fill_price"] = fills.get("entry_fill_price")
        out["exit_signal_price"] = fills.get("exit_signal_price")
        out["exit_fill_price"] = fills.get("exit_fill_price")
        out["entry_slippage_points"] = fills.get("entry_slippage_points")
        out["exit_slippage_points"] = fills.get("exit_slippage_points")
        out["entry_fee_points"] = fills.get("entry_fee_points")
        out["exit_fee_points"] = fills.get("exit_fee_points")
        out["computed_fees_points"] = (
            _safe_float(fills.get("entry_fee_points"))
            + _safe_float(fills.get("exit_fee_points"))
        )
        out["computed_slippage_points"] = (
            _safe_float(fills.get("entry_slippage_points"))
            + _safe_float(fills.get("exit_slippage_points"))
        )

        out["entry_bar"] = int(bt.get("entry_bar", -1))
        out["exit_bar"] = int(bt.get("exit_bar", -1))
        for label, bar_idx, expected_time in (
            ("entry", out["entry_bar"], trade.get("entry_time")),
            ("exit", out["exit_bar"], trade.get("exit_time")),
        ):
            if bar_idx < 0 or bar_idx >= len(replay):
                raise TradeReviewError(f"{label}_bar 越界: {bar_idx}")
            actual_time = _format_dt(replay.iloc[bar_idx]["datetime"])
            if actual_time != _safe_str(expected_time)[:19]:
                raise TradeReviewError(
                    f"回放 {label}_bar 时间不一致: bar={bar_idx}, "
                    f"replay={actual_time}, holdout={expected_time}"
                )
        out["grade"] = _safe_str(bt.get("grade"))
        out["event_id"] = _safe_str(bt.get("event_id"))
        out["signal_key"] = _safe_str(bt.get("signal_key"))
        out["decision_id"] = _safe_str(bt.get("decision_id"))
        out["policy_id"] = trace.get("policy_id", "")
        out["bsp_type"] = trace.get("bsp_type", _safe_str(bt.get("bsp_type")))
        out["setup_invalidation_price"] = trace.get(
            "setup_invalidation_price",
            bt.get("setup_invalidation_price"),
        )
        out["execution_stop_price"] = trace.get(
            "execution_stop_price",
            bt.get("execution_stop_price"),
        )
        out["exit_rule"] = exit_ev.get("exit_rule", bt.get("exit_rule", ""))
        out["exit_trigger_price"] = exit_ev.get("exit_trigger_price")
        out["backtest_pnl_points"] = _safe_float(bt.get("pnl_points"))

        # decision_trace 全字段
        for k, v in trace.items():
            out[f"dt_{k}"] = v
        out["decision_trace"] = trace

        if out["entry_price"] is None or out["exit_price"] is None:
            if not allow_inferred:
                raise TradeReviewError(
                    f"交易 {trade.get('entry_time')} 缺少真实 fill 价"
                    "（默认禁止静默推断）"
                )
            out = _infer_from_close(out, replay)
            out["price_source"] = "close_inferred"
            out["cost_check"] = "N/A"
            out["cost_check_detail"] = "matched trade but fill price inferred from close"
            out["replay_match_check"] = "ok"
            out["decision_trace_check"] = "ok"
            enriched.append(out)
            continue

        # 校验状态
        cost_ok, cost_detail = _backtest_audit_pnl(trade, bt, fills)
        out["cost_check"] = "ok" if cost_ok else "FAIL"
        out["cost_check_detail"] = cost_detail
        out["price_source"] = "backtest"
        out["replay_match_check"] = "ok"
        out["decision_trace_check"] = "ok"

        if not cost_ok:
            raise TradeReviewError(
                f"交易 {trade.get('entry_time')} 回放成本/PnL 不一致: {cost_detail}"
            )
        enriched.append(out)
    return enriched


def _infer_from_close(trade: dict, frame: pd.DataFrame) -> dict:
    entry_idx = find_bar_index(frame, trade.get("entry_time"), "entry")
    exit_idx = find_bar_index(frame, trade.get("exit_time"), "exit")
    trade["entry_price"] = _safe_float(frame.iloc[entry_idx]["close"])
    trade["exit_price"] = _safe_float(frame.iloc[exit_idx]["close"])
    trade["entry_signal_price"] = trade["entry_price"]
    trade["entry_fill_price"] = trade["entry_price"]
    trade["exit_signal_price"] = trade["exit_price"]
    trade["exit_fill_price"] = trade["exit_price"]
    trade["entry_bar"] = entry_idx
    trade["exit_bar"] = exit_idx
    return trade


def _structure_counts(chan) -> dict:
    from Common.CEnum import KL_TYPE

    kl = chan[KL_TYPE.K_15M]
    bsp_count = 0
    if kl.bs_point_lst is not None:
        bsp_count = len(list(kl.bs_point_lst.bsp_iter()))
    return {
        "bi_count": len(kl.bi_list),
        "seg_count": len(kl.seg_list),
        "zs_count": len(kl.zs_list),
        "bsp_count": bsp_count,
    }


def _chan_cutoff_audit(chan, replay: pd.DataFrame, cutoff_idx: int) -> dict:
    """Prove that a snapshot contains no KLU after its declared cutoff."""
    kl = chan[0]
    if not kl.lst or not kl.lst[-1].lst:
        raise TradeReviewError("CChan 快照为空")
    all_klus = [klu for klc in kl.lst for klu in klc.lst]
    last = all_klus[-1]
    expected_dt = _format_dt(replay.iloc[cutoff_idx]["datetime"])
    actual_dt = _format_dt(
        pd.Timestamp(
            year=last.time.year,
            month=last.time.month,
            day=last.time.day,
            hour=last.time.hour,
            minute=last.time.minute,
            second=last.time.second,
        )
    )
    if last.idx != cutoff_idx or len(all_klus) != cutoff_idx + 1:
        raise TradeReviewError(
            "CChan 快照索引不诚实: "
            f"cutoff={cutoff_idx}, last_klu={last.idx}, klu_count={len(all_klus)}"
        )
    if actual_dt != expected_dt:
        raise TradeReviewError(
            f"CChan 快照时间不诚实: expected={expected_dt}, actual={actual_dt}"
        )
    return {
        "cutoff_index": cutoff_idx,
        "cutoff_datetime": expected_dt,
        "last_klu_index": last.idx,
        "last_klu_datetime": actual_dt,
        "klu_count": len(all_klus),
        **_structure_counts(chan),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 画图
# ═══════════════════════════════════════════════════════════════════════════════


def build_display_chan(replay: pd.DataFrame, chan_config: dict, cutoff_idx: int):
    """Build one point-in-time CChan snapshot ending exactly at cutoff_idx."""
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE
    from chan_futures.feed import dataframe_to_klu_iter

    if cutoff_idx < 0 or cutoff_idx >= len(replay):
        raise TradeReviewError(
            f"CChan cutoff 越界: {cutoff_idx}, replay bars={len(replay)}"
        )
    chan = CChan(
        code="RB",
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[KL_TYPE.K_15M],
        config=CChanConfig(dict(chan_config)),
        autype=AUTYPE.NONE,
    )
    for klu in dataframe_to_klu_iter(replay.iloc[: cutoff_idx + 1], kl_type=KL_TYPE.K_15M):
        chan.trigger_load({KL_TYPE.K_15M: [klu]})
    _chan_cutoff_audit(chan, replay, cutoff_idx)
    return chan


def _draw_native_bsp(price_ax, chan, x_begin: int, cutoff_idx: int) -> None:
    """Draw native Chan BSPs in neutral gray so they cannot mimic trade markers."""
    kl_meta = chan[0].bs_point_lst
    if kl_meta is None:
        return
    for bsp in kl_meta.bsp_iter():
        x = bsp.klu.idx
        if x < x_begin or x > cutoff_idx:
            continue
        y = bsp.klu.low if bsp.is_buy else bsp.klu.high
        price_ax.scatter([x], [y], marker="D", s=22, color="0.38", zorder=6, alpha=0.9)
        price_ax.annotate(
            bsp.type2str(),
            xy=(x, y),
            xytext=(0, -11 if bsp.is_buy else 7),
            textcoords="offset points",
            fontsize=6,
            color="0.35",
            ha="center",
            va="top" if bsp.is_buy else "bottom",
        )


def _snapshot_panel(
    *,
    trade: dict,
    replay: pd.DataFrame,
    entry_idx: int,
    exit_idx: int,
    x_begin: int,
    cutoff_idx: int,
    chan_config: dict,
    snapshot_kind: str,
    is_inferred: bool,
    requested_post_bars: int,
):
    """Render one independently computed point-in-time panel to a PIL image."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    _apply_cjk_font_before_plot()
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from PIL import Image

    from Plot.PlotDriver import CPlotDriver
    from chan_futures.rb_chan_plot import DEFAULT_PLOT_CONFIG

    chan = build_display_chan(replay, chan_config, cutoff_idx)
    audit = _chan_cutoff_audit(chan, replay, cutoff_idx)
    display_bars = cutoff_idx - x_begin + 1
    if display_bars <= 0:
        raise TradeReviewError(
            f"快照窗口无效: begin={x_begin}, cutoff={cutoff_idx}"
        )

    plot_config = dict(DEFAULT_PLOT_CONFIG)
    plot_config["plot_macd"] = True
    plot_config["plot_seg"] = True
    plot_config["plot_bsp"] = False
    plot_para = {
        "figure": {
            "x_range": int(display_bars),
            "x_tick_num": 10,
            "grid": "xy",
        }
    }
    driver = CPlotDriver(chan, plot_config=plot_config, plot_para=plot_para)
    fig = driver.figure
    axes = fig.axes
    if len(axes) < 2:
        plt.close(fig)
        raise TradeReviewError("复盘图缺少价格或 MACD 子图")

    price_ax, macd_ax = axes[0], axes[1]
    entry_color = "#1b7f3b"
    exit_color = "#c0392b"
    native_color = "0.38"
    pnl = _safe_float(trade.get("net_pnl_points"))
    is_win = pnl > 0
    entry_fill = _safe_float(trade.get("entry_fill_price"))
    entry_sig = _safe_float(trade.get("entry_signal_price"))
    exit_fill = _safe_float(trade.get("exit_fill_price"))
    exit_sig = _safe_float(trade.get("exit_signal_price"))
    setup_inv = _safe_float(trade.get("setup_invalidation_price"))
    exec_stop = _safe_float(trade.get("execution_stop_price"))
    y_min, y_max = price_ax.get_ylim()
    y_span = max(y_max - y_min, 1.0)

    try:
        _draw_native_bsp(price_ax, chan, x_begin, cutoff_idx)

        if entry_idx <= cutoff_idx:
            for ax in (price_ax, macd_ax):
                ax.axvline(x=entry_idx, color=entry_color, lw=1.4, ls="--", alpha=0.9)
            hold_end = min(exit_idx, cutoff_idx)
            if hold_end > entry_idx:
                hold_shade = "#d8f5dc" if is_win else "#fbdcd6"
                price_ax.axvspan(entry_idx, hold_end, alpha=0.15, color=hold_shade, zorder=0)
            line_end = hold_end if hold_end > entry_idx else entry_idx + 0.8
            price_ax.plot(
                [entry_idx, line_end], [entry_fill, entry_fill],
                color=entry_color, lw=1.6, ls="-", alpha=0.95, zorder=7,
            )
            if abs(entry_sig - entry_fill) > 1e-9:
                price_ax.plot(
                    [entry_idx, line_end], [entry_sig, entry_sig],
                    color=entry_color, lw=0.9, ls=":", alpha=0.75, zorder=6,
                )
            entry_text_x = max(x_begin + 2, entry_idx - 12)
            sig_note = (
                f" sig={entry_sig:.1f}" if abs(entry_sig - entry_fill) > 1e-9 else ""
            )
            price_ax.annotate(
                f"TRADE ENTRY\nfill={entry_fill:.1f}{sig_note}",
                xy=(entry_idx, entry_fill),
                xytext=(entry_text_x, entry_fill - y_span * 0.04),
                fontsize=8,
                color=entry_color,
                fontweight="bold",
                ha="right",
                arrowprops=dict(arrowstyle="->", color=entry_color, lw=1.0),
            )
            stop_start = entry_idx
            stop_end = line_end
            if exec_stop > 0:
                price_ax.plot(
                    [stop_start, stop_end], [exec_stop, exec_stop],
                    color="#e67e22", lw=1.0, ls="-.", alpha=0.9, zorder=6,
                )
                price_ax.text(
                    stop_start, exec_stop, f" stop={exec_stop:.1f}",
                    fontsize=7, color="#e67e22", va="bottom", ha="left",
                )
            if setup_inv > 0:
                price_ax.plot(
                    [stop_start, stop_end], [setup_inv, setup_inv],
                    color="#8e44ad", lw=1.0, ls="-.", alpha=0.9, zorder=6,
                )
                price_ax.text(
                    stop_start, setup_inv, f" inval={setup_inv:.1f}",
                    fontsize=7, color="#8e44ad", va="top", ha="left",
                )

        if exit_idx <= cutoff_idx:
            for ax in (price_ax, macd_ax):
                ax.axvline(x=exit_idx, color=exit_color, lw=1.4, ls="--", alpha=0.9)
            price_ax.plot(
                [max(x_begin, exit_idx - 1), exit_idx], [exit_fill, exit_fill],
                color=exit_color, lw=1.6, ls="-", alpha=0.95, zorder=7,
            )
            exit_note = (
                f" sig={exit_sig:.1f}" if abs(exit_sig - exit_fill) > 1e-9 else ""
            )
            price_ax.annotate(
                f"TRADE EXIT\nfill={exit_fill:.1f}{exit_note}",
                xy=(exit_idx, exit_fill),
                xytext=(max(x_begin + 2, exit_idx - 5), exit_fill + y_span * 0.09),
                fontsize=8,
                color=exit_color,
                fontweight="bold",
                ha="right",
                arrowprops=dict(arrowstyle="->", color=exit_color, lw=1.0),
            )

        if snapshot_kind == "context" and cutoff_idx > exit_idx:
            price_ax.axvspan(
                exit_idx + 1,
                cutoff_idx + 1,
                facecolor="0.7",
                alpha=0.18,
                hatch="///",
                zorder=1,
            )
            mid_future = (exit_idx + 1 + cutoff_idx + 1) / 2
            price_ax.text(
                mid_future,
                0.5,
                f"HINDSIGHT ONLY ({cutoff_idx - exit_idx} post-exit bars)",
                transform=price_ax.get_xaxis_transform(),
                fontsize=8,
                color="#555555",
                ha="center",
                va="center",
                fontstyle="italic",
            )

        legend_handles = [
            Line2D([0], [0], marker="D", ls="", color=native_color, label="Chan BSP (native)"),
            Line2D([0], [0], color=entry_color, ls="--", label="TRADE ENTRY"),
        ]
        if exit_idx <= cutoff_idx:
            legend_handles.append(
                Line2D([0], [0], color=exit_color, ls="--", label="TRADE EXIT")
            )
        price_ax.legend(handles=legend_handles, loc="upper left", fontsize=8, framealpha=0.9)

        if snapshot_kind == "entry":
            decision_lines = [
                "ENTRY-TIME EVIDENCE",
                f"direction={_safe_str(trade.get('direction'))}",
                f"BSP={_safe_str(trade.get('bsp_type'))} grade={_safe_str(trade.get('grade'))}",
                f"signal/fill={entry_sig:.1f}/{entry_fill:.1f}",
                f"stop/inval={exec_stop:.1f}/{setup_inv:.1f}",
                f"regime={_safe_str(trade.get('dt_regime'))}",
                f"parent={_safe_str(trade.get('dt_parent_direction'))}",
                f"child_confirmed={_safe_str(trade.get('dt_child_confirmed'))}",
                f"momentum={_safe_str(trade.get('dt_momentum_status'))}",
            ]
            price_ax.text(
                1.01,
                0.95,
                "\n".join(decision_lines),
                transform=price_ax.transAxes,
                fontsize=8,
                fontfamily="monospace",
                va="top",
                ha="left",
                clip_on=False,
                bbox=dict(boxstyle="round,pad=0.4", facecolor="#eaf2f8", alpha=0.92),
            )
        else:
            pnl_lines = [
                f"Net PnL: {pnl:+.1f} pts",
                f"Entry sig/fill: {entry_sig:.1f}/{entry_fill:.1f}",
                f"Exit sig/fill: {exit_sig:.1f}/{exit_fill:.1f}",
                f"Gross: {_safe_float(trade.get('gross_pnl_points')):+.1f}  "
                f"Fees: {_safe_float(trade.get('fees_points')):.1f}  "
                f"Slip: {_safe_float(trade.get('slippage_points')):.1f}",
                f"Exit: {_safe_str(trade.get('exit_reason'))}",
            ]
            price_ax.text(
                1.01,
                0.95,
                "\n".join(pnl_lines),
                transform=price_ax.transAxes,
                fontsize=8,
                fontfamily="monospace",
                va="top",
                ha="left",
                clip_on=False,
                bbox=dict(
                    boxstyle="round,pad=0.4",
                    facecolor="#d9ead3" if is_win else "#f4cccc",
                    alpha=0.92,
                ),
            )

        cutoff_dt = audit["cutoff_datetime"]
        if snapshot_kind == "entry":
            panel_title = f"A. ENTRY SNAPSHOT | cutoff={cutoff_dt} | NO FUTURE BARS"
        elif snapshot_kind == "exit":
            panel_title = f"B. EXIT SNAPSHOT | cutoff={cutoff_dt} | NO POST-EXIT BARS"
        else:
            actual_post_bars = cutoff_idx - exit_idx
            truncated = actual_post_bars < requested_post_bars
            truncation_note = " | TRUNCATED: DATA ENDED" if truncated else ""
            panel_title = (
                f"C. HINDSIGHT CONTEXT | cutoff={cutoff_dt} | "
                f"POST-EXIT BARS={actual_post_bars}/{requested_post_bars}"
                f"{truncation_note}"
            )
        fig.suptitle(panel_title, fontsize=11, fontweight="bold", y=0.995)

        if is_inferred:
            for ax in (price_ax, macd_ax):
                ax.text(
                    0.5,
                    0.5,
                    _WATERMARK,
                    transform=ax.transAxes,
                    fontsize=24,
                    fontweight="bold",
                    color="red",
                    alpha=0.35,
                    ha="center",
                    va="center",
                    rotation=18,
                    zorder=100,
                )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        buf.seek(0)
        panel = Image.open(buf).convert("RGB").copy()
        buf.close()
    finally:
        plt.close(fig)

    return panel, audit


def _stack_snapshot_panels(panels: list, output_path: str | Path) -> Path:
    from PIL import Image

    gap = 12
    width = max(panel.width for panel in panels)
    height = sum(panel.height for panel in panels) + gap * (len(panels) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for panel in panels:
        x = (width - panel.width) // 2
        canvas.paste(panel, (x, y))
        y += panel.height + gap
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG")
    for panel in panels:
        panel.close()
    canvas.close()
    return output


def render_trade_review_image(
    *,
    trade: dict,
    replay: pd.DataFrame,
    entry_idx: int,
    exit_idx: int,
    plot_start: int,
    plot_end: int,
    chan_config: dict,
    is_inferred: bool,
    requested_post_bars: int,
    output_path: str | Path,
) -> dict:
    """Render entry, exit and hindsight snapshots into one auditable image."""
    entry_time = str(trade.get("entry_time", ""))
    exit_time = str(trade.get("exit_time", ""))

    # ── 坐标诚实断言：索引对应 datetime 必须与 holdout 完全相等 ──
    entry_dt_actual = replay.iloc[entry_idx]["datetime"]
    exit_dt_actual = replay.iloc[exit_idx]["datetime"]
    if str(entry_dt_actual)[:19] != entry_time[:19]:
        raise TradeReviewError(
            f"entry 坐标 datetime 不匹配: replay={entry_dt_actual} holdout={entry_time}"
        )
    if str(exit_dt_actual)[:19] != exit_time[:19]:
        raise TradeReviewError(
            f"exit 坐标 datetime 不匹配: replay={exit_dt_actual} holdout={exit_time}"
        )

    entry_panel, entry_audit = _snapshot_panel(
        trade=trade,
        replay=replay,
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        x_begin=plot_start,
        cutoff_idx=entry_idx,
        chan_config=chan_config,
        snapshot_kind="entry",
        is_inferred=is_inferred,
        requested_post_bars=requested_post_bars,
    )
    exit_panel, exit_audit = _snapshot_panel(
        trade=trade,
        replay=replay,
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        x_begin=plot_start,
        cutoff_idx=exit_idx,
        chan_config=chan_config,
        snapshot_kind="exit",
        is_inferred=is_inferred,
        requested_post_bars=requested_post_bars,
    )
    context_panel, context_audit = _snapshot_panel(
        trade=trade,
        replay=replay,
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        x_begin=plot_start,
        cutoff_idx=plot_end,
        chan_config=chan_config,
        snapshot_kind="context",
        is_inferred=is_inferred,
        requested_post_bars=requested_post_bars,
    )
    output = _stack_snapshot_panels(
        [entry_panel, exit_panel, context_panel],
        output_path,
    )
    checks = (
        entry_audit["cutoff_datetime"] == entry_time[:19]
        and exit_audit["cutoff_datetime"] == exit_time[:19]
        and entry_audit["last_klu_index"] == entry_idx
        and exit_audit["last_klu_index"] == exit_idx
        and context_audit["last_klu_index"] == plot_end
    )
    if not checks:
        raise TradeReviewError("三时点快照截止时间校验失败")
    actual_post_bars = plot_end - exit_idx
    return {
        "path": str(output),
        "snapshot_time_check": "ok",
        "entry_snapshot": entry_audit,
        "exit_snapshot": exit_audit,
        "context_snapshot": context_audit,
        "context_post_exit_bars": actual_post_bars,
        "context_requested_post_exit_bars": requested_post_bars,
        "context_truncated": actual_post_bars < requested_post_bars,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 文件名 / 输出
# ═══════════════════════════════════════════════════════════════════════════════


def make_trade_filename(trade: dict, orig_index: int) -> str:
    pnl = _safe_float(trade.get("net_pnl_points"))
    entry_time = str(trade.get("entry_time", "unknown"))[:10].replace("-", "")
    exit_reason = _safe_str(trade.get("exit_reason"))
    wl = "W" if pnl > 0 else "L"
    seq = f"{wl}{orig_index:02d}"
    safe_reason = exit_reason.replace("/", "_").replace("\\", "_").replace(":", "_")
    return f"{seq}_{entry_time}_{pnl:+.1f}pts_{safe_reason}.png"


def _format_dt(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(_naive_ts(value))[:19]
    except Exception:
        return str(value)


# ═══════════════════════════════════════════════════════════════════════════════
# Manifest / HTML / 元数据
# ═══════════════════════════════════════════════════════════════════════════════


_MANIFEST_FIELDS = [
    "orig_index", "filename", "status",
    "entry_time", "exit_time", "direction",
    "grade", "bsp_type",
    "event_id", "signal_key", "decision_id", "policy_id",
    "entry_signal_price", "entry_fill_price",
    "exit_signal_price", "exit_fill_price",
    "entry_price", "exit_price",
    "entry_slippage_points", "exit_slippage_points",
    "entry_fee_points", "exit_fee_points",
    "gross_pnl_points", "fees_points", "slippage_points", "net_pnl_points",
    "backtest_pnl_points", "replay_match_check", "decision_trace_check",
    "cost_check", "cost_check_detail",
    "exit_reason", "exit_rule", "exit_trigger_price",
    "setup_invalidation_price", "execution_stop_price",
    "dt_regime", "dt_parent_direction", "dt_child_confirmed",
    "dt_momentum_status", "dt_macd_area_ratio", "dt_histogram_state",
    "dt_multi_level_accepted", "dt_multi_level_time_honest",
    "dt_parent_available_at", "dt_child_window_begin", "dt_child_window_end",
    "dt_momentum_available_at",
    "price_source",
    "watermark",
    "marker_entry_datetime", "marker_exit_datetime", "marker_dt_match",
    "snapshot_time_check",
    "entry_snapshot_cutoff_index", "entry_snapshot_cutoff_datetime",
    "entry_snapshot_last_klu_index", "entry_snapshot_last_klu_datetime",
    "entry_snapshot_structure_counts",
    "exit_snapshot_cutoff_index", "exit_snapshot_cutoff_datetime",
    "exit_snapshot_last_klu_index", "exit_snapshot_last_klu_datetime",
    "exit_snapshot_structure_counts",
    "context_snapshot_cutoff_index", "context_snapshot_cutoff_datetime",
    "context_snapshot_last_klu_index", "context_snapshot_last_klu_datetime",
    "context_snapshot_structure_counts", "context_post_exit_bars",
    "context_requested_post_exit_bars", "context_truncated",
    "config_sha256", "data_15m_sha256", "data_60m_sha256", "data_5m_sha256",
    "report_git_head", "current_git_head",
    "scenario_index", "scenario_name", "fee_multiplier", "slippage_multiplier",
]


def _flatten_trade_row(result: dict, meta: dict) -> dict:
    trade = result.get("trade", {})
    row: dict[str, Any] = {
        "orig_index": trade.get("_orig_index", result.get("index", "")),
        "filename": result.get("filename", ""),
        "status": result.get("status", ""),
        "entry_time": _safe_str(trade.get("entry_time")),
        "exit_time": _safe_str(trade.get("exit_time")),
        "direction": _safe_str(trade.get("direction")),
        "grade": _safe_str(trade.get("grade")),
        "bsp_type": _safe_str(trade.get("bsp_type")),
        "event_id": _safe_str(trade.get("event_id")),
        "signal_key": _safe_str(trade.get("signal_key")),
        "decision_id": _safe_str(trade.get("decision_id")),
        "policy_id": _safe_str(trade.get("policy_id")),
        "entry_signal_price": trade.get("entry_signal_price", ""),
        "entry_fill_price": trade.get("entry_fill_price", ""),
        "exit_signal_price": trade.get("exit_signal_price", ""),
        "exit_fill_price": trade.get("exit_fill_price", ""),
        "entry_price": trade.get("entry_price", ""),
        "exit_price": trade.get("exit_price", ""),
        "entry_slippage_points": trade.get("entry_slippage_points", ""),
        "exit_slippage_points": trade.get("exit_slippage_points", ""),
        "entry_fee_points": trade.get("entry_fee_points", ""),
        "exit_fee_points": trade.get("exit_fee_points", ""),
        "gross_pnl_points": trade.get("gross_pnl_points", ""),
        "fees_points": trade.get("fees_points", ""),
        "slippage_points": trade.get("slippage_points", ""),
        "net_pnl_points": trade.get("net_pnl_points", ""),
        "backtest_pnl_points": trade.get("backtest_pnl_points", ""),
        "replay_match_check": trade.get("replay_match_check", ""),
        "decision_trace_check": trade.get("decision_trace_check", ""),
        "cost_check": trade.get("cost_check", ""),
        "cost_check_detail": trade.get("cost_check_detail", ""),
        "exit_reason": _safe_str(trade.get("exit_reason")),
        "exit_rule": _safe_str(trade.get("exit_rule")),
        "exit_trigger_price": trade.get("exit_trigger_price", ""),
        "setup_invalidation_price": trade.get("setup_invalidation_price", ""),
        "execution_stop_price": trade.get("execution_stop_price", ""),
        "dt_regime": _safe_str(trade.get("dt_regime")),
        "dt_parent_direction": _safe_str(trade.get("dt_parent_direction")),
        "dt_child_confirmed": trade.get("dt_child_confirmed", ""),
        "dt_momentum_status": _safe_str(trade.get("dt_momentum_status")),
        "dt_macd_area_ratio": trade.get("dt_macd_area_ratio", ""),
        "dt_histogram_state": _safe_str(trade.get("dt_histogram_state")),
        "dt_multi_level_accepted": trade.get("dt_multi_level_accepted", ""),
        "dt_multi_level_time_honest": trade.get("dt_multi_level_time_honest", ""),
        "dt_parent_available_at": _format_dt(trade.get("dt_parent_available_at")),
        "dt_child_window_begin": _format_dt(trade.get("dt_child_window_begin")),
        "dt_child_window_end": _format_dt(trade.get("dt_child_window_end")),
        "dt_momentum_available_at": _format_dt(trade.get("dt_momentum_available_at")),
        "price_source": _safe_str(trade.get("price_source")),
        "watermark": (
            _WATERMARK if _safe_str(trade.get("price_source")) != "backtest" else ""
        ),
        "marker_entry_datetime": result.get("marker_entry_datetime", ""),
        "marker_exit_datetime": result.get("marker_exit_datetime", ""),
        "marker_dt_match": result.get("marker_dt_match", ""),
        "snapshot_time_check": trade.get("snapshot_time_check", ""),
        "entry_snapshot_cutoff_index": trade.get("entry_snapshot_cutoff_index", ""),
        "entry_snapshot_cutoff_datetime": trade.get("entry_snapshot_cutoff_datetime", ""),
        "entry_snapshot_last_klu_index": trade.get("entry_snapshot_last_klu_index", ""),
        "entry_snapshot_last_klu_datetime": trade.get("entry_snapshot_last_klu_datetime", ""),
        "entry_snapshot_structure_counts": trade.get("entry_snapshot_structure_counts", ""),
        "exit_snapshot_cutoff_index": trade.get("exit_snapshot_cutoff_index", ""),
        "exit_snapshot_cutoff_datetime": trade.get("exit_snapshot_cutoff_datetime", ""),
        "exit_snapshot_last_klu_index": trade.get("exit_snapshot_last_klu_index", ""),
        "exit_snapshot_last_klu_datetime": trade.get("exit_snapshot_last_klu_datetime", ""),
        "exit_snapshot_structure_counts": trade.get("exit_snapshot_structure_counts", ""),
        "context_snapshot_cutoff_index": trade.get("context_snapshot_cutoff_index", ""),
        "context_snapshot_cutoff_datetime": trade.get("context_snapshot_cutoff_datetime", ""),
        "context_snapshot_last_klu_index": trade.get("context_snapshot_last_klu_index", ""),
        "context_snapshot_last_klu_datetime": trade.get("context_snapshot_last_klu_datetime", ""),
        "context_snapshot_structure_counts": trade.get("context_snapshot_structure_counts", ""),
        "context_post_exit_bars": trade.get("context_post_exit_bars", ""),
        "context_requested_post_exit_bars": trade.get(
            "context_requested_post_exit_bars", ""
        ),
        "context_truncated": trade.get("context_truncated", ""),
        "config_sha256": meta.get("config_sha256", ""),
        "data_15m_sha256": meta.get("data_sha256", {}).get("15m", ""),
        "data_60m_sha256": meta.get("data_sha256", {}).get("60m", ""),
        "data_5m_sha256": meta.get("data_sha256", {}).get("5m", ""),
        "report_git_head": meta.get("report_git_head", ""),
        "current_git_head": meta.get("current_git_head", ""),
        "scenario_index": meta.get("scenario_index", ""),
        "scenario_name": meta.get("scenario_name", ""),
        "fee_multiplier": meta.get("fee_multiplier", ""),
        "slippage_multiplier": meta.get("slippage_multiplier", ""),
    }
    return row


def save_manifest(results: list[dict], output_dir: Path, meta: dict) -> Path:
    path = output_dir / "trade_review_manifest.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(_flatten_trade_row(r, meta))
    return path


def save_run_metadata(output_dir: Path, meta: dict) -> Path:
    path = output_dir / "run_meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def save_trade_json(result: dict, output_dir: Path) -> Path:
    trade = result.get("trade", {})
    idx = trade.get("_orig_index", result.get("index", ""))
    pnl = _safe_float(trade.get("net_pnl_points"))
    wl = "W" if pnl > 0 else "L"
    name = f"{wl}{idx:02d}"
    path = output_dir / f"{name}.json"
    payload = dict(trade)
    payload.setdefault("output_file", result.get("filename", ""))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def save_index_html(results: list[dict], output_dir: Path, meta: dict, is_inferred: bool) -> Path:
    """HTML 只展示成功且来源校验通过的图。"""
    path = output_dir / "index.html"

    def _ok(r: dict) -> bool:
        if r.get("status") != "ok":
            return False
        trade = r.get("trade", {})
        source = trade.get("price_source")
        exact_ok = (
            source == "backtest"
            and trade.get("replay_match_check") == "ok"
            and trade.get("decision_trace_check") == "ok"
            and trade.get("cost_check") == "ok"
        )
        inferred_ok = source == "close_inferred" and bool(trade.get("watermark", _WATERMARK))
        return (
            (exact_ok or inferred_ok)
            and trade.get("snapshot_time_check") == "ok"
            and r.get("marker_dt_match") == "ok"
        )

    shown = [r for r in results if _ok(r)]
    cards_html = ""
    for r in shown:
        trade = r.get("trade", {})
        pnl = _safe_float(trade.get("net_pnl_points"))
        is_win = pnl > 0
        wl_class = "win" if is_win else "loss"
        wl_label = "W" if is_win else "L"
        badge_color = "#27ae60" if is_win else "#e74c3c"
        filename = html_lib.escape(_safe_str(r.get("filename")), quote=True)
        exit_reason = html_lib.escape(_safe_str(trade.get("exit_reason")))
        entry_time = html_lib.escape(_safe_str(trade.get("entry_time")))
        exit_time = html_lib.escape(_safe_str(trade.get("exit_time")))
        direction = html_lib.escape(_safe_str(trade.get("direction")))
        entry_fill = html_lib.escape(_safe_str(trade.get("entry_fill_price")))
        exit_fill = html_lib.escape(_safe_str(trade.get("exit_fill_price")))
        price_source = html_lib.escape(_safe_str(trade.get("price_source")))
        cards_html += f"""
        <div class="card {wl_class}">
            <div class="card-header">
                <span class="badge" style="background:{badge_color}">{wl_label}{trade.get('_orig_index', r.get('index', '')):02d}</span>
                <span class="pnl">{pnl:+.1f} pts</span>
                <span class="reason">{exit_reason}</span>
            </div>
            <a href="{filename}" target="_blank">
                <img src="{filename}" loading="lazy" alt="{filename}" />
            </a>
            <div class="card-footer">
                {entry_time} → {exit_time}
                &nbsp;|&nbsp; {direction}
                &nbsp;|&nbsp; entry={entry_fill}
                &nbsp;|&nbsp; exit={exit_fill}
                &nbsp;|&nbsp; src={price_source}
            </div>
        </div>"""

    inferred_html = ""
    if is_inferred:
        inferred_html = (
            '<div class="watermark-banner">⚠ 本批次使用了 K 线 close 推断价格 '
            f"(INFERRED / NOT ACTUAL FILL)。</div>"
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>交易复盘图表 V2 | Trade Review</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f5f5; color: #333; }}
  .header {{ background: #2c3e50; color: #fff; padding: 20px 30px; }}
  .header h1 {{ font-size: 20px; }}
  .header p {{ font-size: 13px; opacity: 0.75; margin-top: 4px; }}
  .watermark-banner {{
    background: repeating-linear-gradient(45deg, #fff5f5, #fff5f5 12px, #ffe6e6 12px, #ffe6e6 24px);
    border: 2px solid #e74c3c; color: #e74c3c; font-weight: bold;
    padding: 10px 30px; margin: 10px 30px; border-radius: 6px;
  }}
  .filters {{ padding: 15px 30px; display: flex; gap: 10px; flex-wrap: wrap; }}
  .filters button {{ padding: 6px 16px; border: 1px solid #ccc; border-radius: 4px; background: #fff; cursor: pointer; font-size: 13px; }}
  .filters button.active {{ background: #3498db; color: #fff; border-color: #3498db; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 20px; padding: 0 30px 30px; }}
  .card {{ background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
  .card-header {{ padding: 10px 14px; display: flex; align-items: center; gap: 10px; font-size: 13px; border-bottom: 1px solid #eee; }}
  .badge {{ color: #fff; padding: 2px 8px; border-radius: 3px; font-weight: bold; font-size: 12px; }}
  .pnl {{ font-weight: bold; font-family: monospace; }}
  .reason {{ color: #666; font-size: 12px; margin-left: auto; }}
  .card img {{ width: 100%; display: block; cursor: pointer; }}
  .card-footer {{ padding: 8px 14px; font-size: 11px; color: #888; border-top: 1px solid #eee; }}
  .card.loss {{ border-left: 3px solid #e74c3c; }}
  .card.win {{ border-left: 3px solid #27ae60; }}
  .stats {{ padding: 5px 30px; font-size: 13px; color: #666; }}
</style>
</head>
<body>
<div class="header">
  <h1>RB 15m 交易复盘图表 V2</h1>
  <p>{len(shown)} 笔通过校验的图（共 {len(results)} 笔） &nbsp;|&nbsp; scenario: {meta.get('scenario_name','')} &nbsp;|&nbsp; 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
</div>

{inferred_html}

<div class="filters">
  <button class="active" onclick="filterCards('all', this)">全部</button>
  <button onclick="filterCards('win', this)">盈利</button>
  <button onclick="filterCards('loss', this)">亏损</button>
</div>

<div class="stats" id="stats"></div>

<div class="grid" id="grid">
{cards_html}
</div>

<script>
  function filterCards(type, btn) {{
    document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const cards = document.querySelectorAll('.card');
    let visible = 0;
    cards.forEach(c => {{
      const show = type === 'all' || c.classList.contains(type);
      c.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    document.getElementById('stats').textContent = '显示 ' + visible + ' / ' + cards.length + ' 张';
  }}
  document.getElementById('stats').textContent = '共 {len(shown)} 张图表（仅成功且来源校验通过）';
</script>
</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════


def run_pipeline(
    *,
    holdout_path: str | Path = DEFAULT_HOLDOUT,
    config_path: str | Path = DEFAULT_CONFIG,
    parquet_15m: str | Path = DEFAULT_DATA_15M,
    parquet_60m: str | Path = DEFAULT_DATA_60M,
    parquet_5m: str | Path = DEFAULT_DATA_5M,
    output_root: str | Path = DEFAULT_OUTPUT_DIR,
    scenario_index: int = 0,
    pnl_filter: str = "all",
    reason_filter: str = "",
    date_start: str = "",
    date_end: str = "",
    pre_bars: int = DEFAULT_PRE_BARS,
    post_bars: int = DEFAULT_POST_BARS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    allow_inferred: bool = False,
    run_tag: str = "",
    orig_indices: list[int] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """完整流程：严格重放 holdout → 真实 fill 补全 → 生成图 → manifest/html。"""
    def log(msg: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("已取消")
        if progress_callback:
            progress_callback(msg)
        else:
            print(msg)

    if pre_bars < 1 or post_bars < 0 or warmup_bars < 1:
        raise TradeReviewError(
            f"窗口参数无效: pre={pre_bars}, post={post_bars}, warmup={warmup_bars}"
        )

    # ── 1. 加载 holdout + scenario ──
    log(f"加载 holdout: {holdout_path}")
    data = load_holdout(holdout_path)
    trades, scenario_meta = select_scenario(data, scenario_index)
    log(f"  scenario #{scenario_index} ({scenario_name(scenario_index)}): "
        f"{len(trades)} 笔交易")

    if 0 <= scenario_index < len(SCENARIO_META):
        default_fee_mult = SCENARIO_META[scenario_index][1]
        default_slip_mult = SCENARIO_META[scenario_index][2]
    else:
        default_fee_mult = default_slip_mult = 1.0
    fee_mult = _safe_float(scenario_meta.get("fee_multiplier", default_fee_mult))
    slip_mult = _safe_float(scenario_meta.get("slippage_multiplier", default_slip_mult))

    trades = assign_original_index(trades)
    all_trades = list(trades)

    # ── 2. 筛选（保留原始全局序号）──
    if date_start:
        s_ts = _naive_ts(date_start)
        trades = [t for t in trades if _naive_ts(t["entry_time"]) >= s_ts]
    if date_end:
        e_ts = _inclusive_end_ts(date_end)
        trades = [t for t in trades if _naive_ts(t["entry_time"]) <= e_ts]
    if pnl_filter == "winning":
        trades = [t for t in trades if _safe_float(t.get("net_pnl_points")) > 0]
    elif pnl_filter == "losing":
        trades = [t for t in trades if _safe_float(t.get("net_pnl_points")) <= 0]
    if reason_filter:
        trades = [t for t in trades if _safe_str(t.get("exit_reason")) == reason_filter]
    if orig_indices is not None:
        wanted = set(int(i) for i in orig_indices)
        trades = [t for t in trades if int(t["_orig_index"]) in wanted]
    log(f"  筛选后 {len(trades)} 笔（原始序号保留: "
        f"{[t['_orig_index'] for t in trades]}）")
    if not trades:
        raise TradeReviewError("没有匹配的交易，退出。")
    selected_orig_indices = {int(t["_orig_index"]) for t in trades}

    # ── 3. 加载数据 ──
    log(f"加载 15m: {parquet_15m}")
    frame_15m = _normalize_datetime(pd.read_parquet(parquet_15m))
    log(f"加载 60m: {parquet_60m}")
    frame_60m = _normalize_datetime(pd.read_parquet(parquet_60m))
    log(f"加载 5m: {parquet_5m}")
    frame_5m = _normalize_datetime(pd.read_parquet(parquet_5m))

    # ── 4. 严格回测重放 ──
    from chan_futures.config_loader import load_config

    config = load_config(config_path)
    oos_start = _safe_str(data.get("oos_start", DEFAULT_OOS_START))
    log(f"严格窗口回测: warmup={warmup_bars}, decision_start={oos_start}, "
        f"fee_mult={fee_mult}, slip_mult={slip_mult}")
    replay, result = run_replay_backtest(
        config,
        frame_15m=frame_15m,
        frame_60m=frame_60m,
        frame_5m=frame_5m,
        oos_start=oos_start,
        warmup_bars=warmup_bars,
        fee_mult=fee_mult,
        slip_mult=slip_mult,
    )
    log(f"  回测完成: replay {len(replay)} bars, "
        f"{len(result.trades)} 笔交易, "
        f"{len(result.fills)} 条 fill")

    # ── 5. 先核对完整 scenario，再筛选输出 ──
    log("核对完整 scenario 的真实 fill、PnL 与 decision_trace ...")
    all_enriched = enrich_trades(
        all_trades,
        result,
        fee_points=config.execution.fee_points * fee_mult,
        slippage_points=config.execution.slippage_points * slip_mult,
        allow_inferred=allow_inferred,
        replay=replay,
        require_exact_trade_set=True,
    )
    enriched = [
        trade
        for trade in all_enriched
        if int(trade["_orig_index"]) in selected_orig_indices
    ]

    # ── 6. 输出目录（每次运行独立子目录，不覆盖旧结果）──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_name = _sanitize(f"{ts}_{scenario_index}_{run_tag or 'run'}")
    run_dir = Path(output_root) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── 7. 渲染 ──
    chan_config = config.chan.to_dict()
    results: list[dict] = []
    for i, trade in enumerate(enriched):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("已取消")
        orig_idx = int(trade["_orig_index"])
        filename = make_trade_filename(trade, orig_idx)
        output_path = run_dir / filename
        log(f"[{i+1}/{len(enriched)}] {filename} ...")

        win = compute_trade_window(
            replay,
            trade["entry_time"],
            trade["exit_time"],
            pre_bars=pre_bars,
            post_bars=post_bars,
        )
        marker_entry_dt = _format_dt(replay.iloc[win["entry_idx"]]["datetime"])
        marker_exit_dt = _format_dt(replay.iloc[win["exit_idx"]]["datetime"])
        marker_match = (
            "ok"
            if (marker_entry_dt == _safe_str(trade["entry_time"])[:19]
                and marker_exit_dt == _safe_str(trade["exit_time"])[:19])
            else "FAIL"
        )
        try:
            snapshot_meta = render_trade_review_image(
                trade=trade,
                replay=replay,
                entry_idx=win["entry_idx"],
                exit_idx=win["exit_idx"],
                plot_start=win["plot_start"],
                plot_end=win["plot_end"],
                chan_config=chan_config,
                is_inferred=_safe_str(trade.get("price_source")) != "backtest",
                requested_post_bars=post_bars,
                output_path=output_path,
            )
            trade["snapshot_time_check"] = snapshot_meta["snapshot_time_check"]
            trade["context_post_exit_bars"] = snapshot_meta["context_post_exit_bars"]
            trade["context_requested_post_exit_bars"] = snapshot_meta[
                "context_requested_post_exit_bars"
            ]
            trade["context_truncated"] = snapshot_meta["context_truncated"]
            for prefix in ("entry", "exit", "context"):
                audit = snapshot_meta[f"{prefix}_snapshot"]
                trade[f"{prefix}_snapshot_cutoff_index"] = audit["cutoff_index"]
                trade[f"{prefix}_snapshot_cutoff_datetime"] = audit["cutoff_datetime"]
                trade[f"{prefix}_snapshot_last_klu_index"] = audit["last_klu_index"]
                trade[f"{prefix}_snapshot_last_klu_datetime"] = audit["last_klu_datetime"]
                trade[f"{prefix}_snapshot_structure_counts"] = json.dumps(
                    {
                        key: audit[key]
                        for key in ("bi_count", "seg_count", "zs_count", "bsp_count")
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            results.append({
                "index": i + 1,
                "orig_index": orig_idx,
                "filename": filename,
                "output_path": str(output_path),
                "status": "ok",
                "trade": trade,
                "marker_entry_datetime": marker_entry_dt,
                "marker_exit_datetime": marker_exit_dt,
                "marker_dt_match": marker_match,
            })
            log(f"  -> 已保存: {output_path.name}")
        except Exception as exc:
            results.append({
                "index": i + 1,
                "orig_index": orig_idx,
                "filename": filename,
                "output_path": str(output_path),
                "status": f"error: {exc}",
                "trade": trade,
                "marker_entry_datetime": marker_entry_dt,
                "marker_exit_datetime": marker_exit_dt,
                "marker_dt_match": marker_match,
            })
            log(f"  -> 失败: {exc}")

    # ── 8. manifest / meta / trade json / html ──
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config_path": str(config_path),
        "config_sha256": _sha256(_abs(config_path)),
        "data_sha256": {
            "15m": _sha256(_abs(parquet_15m)),
            "60m": _sha256(_abs(parquet_60m)),
            "5m": _sha256(_abs(parquet_5m)),
        },
        "report_git_head": _safe_str(data.get("git_head")),
        "current_git_head": _git_output("rev-parse", "HEAD"),
        "git_status_short": _git_output("status", "--porcelain"),
        "oos_start": oos_start,
        "decision_start": oos_start,
        "warmup_bars": warmup_bars,
        "replay_start": _format_dt(replay["datetime"].iloc[0]),
        "scenario_index": scenario_index,
        "scenario_name": scenario_name(scenario_index),
        "fee_multiplier": fee_mult,
        "slippage_multiplier": slip_mult,
        "allow_inferred": allow_inferred,
        "total_trades": len(all_trades),
        "validated_trades": len(all_enriched),
        "rendered_trades": len(enriched),
    }
    manifest_path = save_manifest(results, run_dir, meta)
    meta_path = save_run_metadata(run_dir, meta)
    for r in results:
        if r["status"] == "ok":
            save_trade_json(r, run_dir)
    inferred_present = any(
        _safe_str(r.get("trade", {}).get("price_source")) != "backtest"
        for r in results
    )
    index_path = save_index_html(results, run_dir, meta, inferred_present)

    ok_count = sum(1 for r in results if r["status"] == "ok")
    err_count = sum(1 for r in results if r["status"].startswith("error"))
    log(f"完成: {ok_count} 生成, {err_count} 失败")
    log(f"输出目录: {run_dir}")
    log(f"Manifest: {manifest_path}")
    log(f"Meta: {meta_path}")
    log(f"Index: {index_path}")

    return {
        "results": results,
        "output_dir": str(run_dir),
        "manifest": str(manifest_path),
        "index": str(index_path),
        "ok": ok_count,
        "errors": err_count,
        "meta": meta,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# tkinter GUI
# ═══════════════════════════════════════════════════════════════════════════════


def _browse_file(var, filetypes: list):
    import tkinter as tk
    from tkinter import filedialog

    path = filedialog.askopenfilename(initialfile=str(var.get()), filetypes=filetypes)
    if path:
        var.set(path)


def _browse_directory(var):
    from tkinter import filedialog

    path = filedialog.askdirectory(initialdir=str(var.get()))
    if path:
        var.set(path)


def launch_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("交易复盘图表生成器 V2 | Trade Review Plotter")
    root.geometry("940x860")

    holdout_var = tk.StringVar(value=str(DEFAULT_HOLDOUT))
    config_var = tk.StringVar(value=str(DEFAULT_CONFIG))
    parquet_15m_var = tk.StringVar(value=str(DEFAULT_DATA_15M))
    parquet_60m_var = tk.StringVar(value=str(DEFAULT_DATA_60M))
    parquet_5m_var = tk.StringVar(value=str(DEFAULT_DATA_5M))
    output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
    scenario_var = tk.StringVar(value="0")
    pnl_var = tk.StringVar(value="all")
    reason_var = tk.StringVar(value="")
    pre_var = tk.IntVar(value=DEFAULT_PRE_BARS)
    post_var = tk.IntVar(value=DEFAULT_POST_BARS)
    warmup_var = tk.IntVar(value=DEFAULT_WARMUP_BARS)
    allow_inferred_var = tk.BooleanVar(value=False)
    date_start_var = tk.StringVar(value="")
    date_end_var = tk.StringVar(value="")

    row = 0

    def _file_row(label, var, filetypes, row_):
        ttk.Label(root, text=label).grid(row=row_, column=0, sticky="w", padx=10, pady=2)
        ttk.Entry(root, textvariable=var, width=64).grid(row=row_, column=1, padx=2, pady=2)
        ttk.Button(root, text="浏览", command=lambda: _browse_file(var, filetypes)).grid(row=row_, column=2, padx=2, pady=2)
        return row_ + 1

    row = _file_row("Holdout JSON:", holdout_var, [("JSON", "*.json")], row)
    row = _file_row("策略配置:", config_var, [("YAML", "*.yaml *.yml"), ("JSON", "*.json")], row)
    row = _file_row("K线 15m:", parquet_15m_var, [("Parquet", "*.parquet")], row)
    row = _file_row("K线 60m:", parquet_60m_var, [("Parquet", "*.parquet")], row)
    row = _file_row("K线 5m:", parquet_5m_var, [("Parquet", "*.parquet")], row)
    ttk.Label(root, text="输出根目录:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=output_var, width=64).grid(row=row, column=1, padx=2, pady=2)
    ttk.Button(root, text="浏览", command=lambda: _browse_directory(output_var)).grid(
        row=row, column=2, padx=2, pady=2
    )
    row += 1

    ttk.Separator(root, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=6)
    row += 1

    # scenario 带名称下拉框
    def _scenario_labels() -> list[str]:
        try:
            data = load_holdout(holdout_var.get())
            names = []
            for i in range(len(data.get("scenarios", []))):
                names.append(f"{i}: {scenario_name(i)}")
            return names or ["0: baseline (cost 1.0x)"]
        except Exception:
            return [f"{i}: {scenario_name(i)}" for i in range(3)]

    scenario_labels = _scenario_labels()
    scenario_var.set(scenario_labels[0])
    ttk.Label(root, text="Scenario:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    scenario_combo = ttk.Combobox(root, textvariable=scenario_var, values=scenario_labels,
                                  state="readonly", width=36)
    scenario_combo.grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    ttk.Label(root, text="盈亏筛选:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    pnl_frame = ttk.Frame(root)
    ttk.Radiobutton(pnl_frame, text="全部", variable=pnl_var, value="all").pack(side="left", padx=5)
    ttk.Radiobutton(pnl_frame, text="盈利", variable=pnl_var, value="winning").pack(side="left", padx=5)
    ttk.Radiobutton(pnl_frame, text="亏损", variable=pnl_var, value="losing").pack(side="left", padx=5)
    pnl_frame.grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    # 出场原因：从 JSON 动态读取
    ttk.Label(root, text="出场原因筛选:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    reason_combo = ttk.Combobox(root, textvariable=reason_var, width=30, state="readonly")
    reason_combo["values"] = [""]
    reason_combo.grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    # 交易多选表
    ttk.Label(root, text="选择交易(可多选; 留空=全部):").grid(row=row, column=0, sticky="nw", padx=10, pady=2)
    trade_list = tk.Listbox(root, height=6, selectmode="extended", exportselection=False)
    trade_list.grid(row=row, column=1, sticky="ew", padx=2, pady=2)
    trade_btns = ttk.Frame(root)
    ttk.Button(trade_btns, text="全选", width=8,
               command=lambda: trade_list.selection_set(0, tk.END)).pack(side="left", padx=2)
    ttk.Button(trade_btns, text="全不选", width=8,
               command=lambda: trade_list.selection_clear(0, tk.END)).pack(side="left", padx=2)
    trade_btns.grid(row=row, column=2, sticky="n", padx=2, pady=2)
    row += 1

    ttk.Label(root, text="日期开始:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=date_start_var, width=24).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1
    ttk.Label(root, text="日期结束:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=date_end_var, width=24).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    ttk.Separator(root, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=6)
    row += 1

    ttk.Label(root, text="Pre bars:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=pre_var, width=8).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1
    ttk.Label(root, text="Post bars (future):").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=post_var, width=8).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1
    ttk.Label(root, text="Warmup bars:").grid(row=row, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(root, textvariable=warmup_var, width=8).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    ttk.Checkbutton(root, text="允许 close 推断价格（危险，会在输出打水印）",
                    variable=allow_inferred_var).grid(row=row, column=1, sticky="w", padx=2, pady=2)
    row += 1

    ttk.Separator(root, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=6)
    row += 1

    # ── 日志 ──
    log_frame = ttk.Frame(root)
    log_frame.grid(row=row, column=0, columnspan=3, sticky="nsew", padx=10, pady=4)
    root.grid_rowconfigure(row, weight=1)
    root.grid_columnconfigure(1, weight=1)

    log_text = tk.Text(log_frame, height=12, wrap="word", font=("Consolas", 10))
    log_scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side="left", fill="both", expand=True)
    log_scroll.pack(side="right", fill="y")

    # ── 动态加载 scenario 交易到列表 + 出场原因 ──
    def _reload_scenario_trades(*_args):
        try:
            data = load_holdout(holdout_var.get())
            idx = int(scenario_var.get().split(":")[0])
            trades, _ = select_scenario(data, idx)
        except Exception as exc:
            trades = []
            log_text.insert(tk.END, f"读取 scenario 失败: {exc}\n")
        trade_list.delete(0, tk.END)
        reasons = set()
        for trade_index, t in enumerate(trades, start=1):
            pnl = _safe_float(t.get("net_pnl_points"))
            wl = "W" if pnl > 0 else "L"
            label = f"{wl}{trade_index:02d}  {t.get('entry_time','')} -> {t.get('exit_time','')}  {pnl:+.1f}pts  {t.get('exit_reason','')}"
            trade_list.insert(tk.END, label)
            if t.get("exit_reason"):
                reasons.add(str(t["exit_reason"]))
        reason_combo["values"] = [""] + sorted(reasons)
        reason_var.set("")

    scenario_combo.bind("<<ComboboxSelected>>", _reload_scenario_trades)
    _reload_scenario_trades()

    cancel_event = threading.Event()
    ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    worker_state: dict[str, threading.Thread | None] = {"thread": None}

    def _append_log(msg: str):
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)

    def _drain_ui_queue():
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "log":
                    _append_log(_safe_str(payload))
                elif kind == "success":
                    _append_log(
                        f"\n=== 完成 === 生成: {payload['ok']}, 失败: {payload['errors']}"
                    )
                    _append_log(f"输出目录: {payload['output_dir']}")
                elif kind == "cancelled":
                    _append_log("\n已取消。")
                elif kind == "error":
                    message, trace = payload
                    _append_log(f"\n错误: {message}")
                    _append_log(trace)
                elif kind == "finished":
                    btn_generate.config(state="normal", text="生成选中图表")
                    btn_cancel.config(state="disabled")
                    worker_state["thread"] = None
        except queue.Empty:
            pass
        root.after(100, _drain_ui_queue)

    def _worker(params: dict[str, Any]):
        try:
            result = run_pipeline(
                **params,
                progress_callback=lambda msg: ui_queue.put(("log", msg)),
                cancel_event=cancel_event,
            )
            ui_queue.put(("success", result))
        except CancelledError:
            ui_queue.put(("cancelled", None))
        except Exception as exc:
            import traceback

            ui_queue.put(("error", (_safe_str(exc), traceback.format_exc())))
        finally:
            ui_queue.put(("finished", None))

    def _on_generate():
        thread = worker_state.get("thread")
        if thread is not None and thread.is_alive():
            return
        try:
            scenario_index = int(scenario_var.get().split(":")[0])
            orig_indices = None
            selected = list(trade_list.curselection())
            if selected:
                data = load_holdout(holdout_var.get())
                scenario_trades, _ = select_scenario(data, scenario_index)
                assigned = assign_original_index(scenario_trades)
                orig_indices = [
                    trade["_orig_index"]
                    for position, trade in enumerate(assigned)
                    if position in selected
                ]
            params = {
                "holdout_path": holdout_var.get(),
                "config_path": config_var.get(),
                "parquet_15m": parquet_15m_var.get(),
                "parquet_60m": parquet_60m_var.get(),
                "parquet_5m": parquet_5m_var.get(),
                "output_root": output_var.get(),
                "scenario_index": scenario_index,
                "pnl_filter": pnl_var.get(),
                "reason_filter": reason_var.get(),
                "date_start": date_start_var.get(),
                "date_end": date_end_var.get(),
                "pre_bars": pre_var.get(),
                "post_bars": post_var.get(),
                "warmup_bars": warmup_var.get(),
                "allow_inferred": allow_inferred_var.get(),
                "run_tag": "gui",
                "orig_indices": orig_indices,
            }
        except Exception as exc:
            messagebox.showerror("参数错误", _safe_str(exc))
            return

        log_text.delete("1.0", tk.END)
        cancel_event.clear()
        btn_generate.config(state="disabled", text="生成中...")
        btn_cancel.config(state="normal")
        thread = threading.Thread(target=_worker, args=(params,), daemon=True)
        worker_state["thread"] = thread
        thread.start()

    def _on_cancel():
        cancel_event.set()
        _append_log("取消请求已发送（将在当前阶段结束后停止）...")

    btn_frame = ttk.Frame(root)
    btn_frame.grid(row=row, column=1, sticky="w", padx=2, pady=6)
    btn_generate = ttk.Button(btn_frame, text="生成选中图表", command=_on_generate)
    btn_generate.pack(side="left", padx=4)
    btn_cancel = ttk.Button(btn_frame, text="取消", command=_on_cancel, state="disabled")
    btn_cancel.pack(side="left", padx=4)

    log_text.insert("1.0", "就绪。选择参数后点击「生成选中图表」。每次运行写入独立子目录。\n")
    root.after(100, _drain_ui_queue)
    root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout", default=str(DEFAULT_HOLDOUT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--parquet-15m", default=str(DEFAULT_DATA_15M))
    parser.add_argument("--parquet-60m", default=str(DEFAULT_DATA_60M))
    parser.add_argument("--parquet-5m", default=str(DEFAULT_DATA_5M))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--scenario", type=int, default=0)
    parser.add_argument("--pnl-filter", choices=["all", "winning", "losing"], default="all")
    parser.add_argument("--reason-filter", default="")
    parser.add_argument("--date-start", default="")
    parser.add_argument("--date-end", default="")
    parser.add_argument("--pre-bars", type=int, default=DEFAULT_PRE_BARS)
    parser.add_argument("--post-bars", type=int, default=DEFAULT_POST_BARS)
    parser.add_argument("--warmup-bars", type=int, default=DEFAULT_WARMUP_BARS)
    parser.add_argument("--run-tag", default="")
    parser.add_argument("--allow-inferred-prices", action="store_true",
                        help="显式允许回放/匹配失败时用 K 线 close 推断价格（会打水印）")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--all", action="store_true", help="等同于 --pnl-filter all")
    args = parser.parse_args()

    if not args.no_ui:
        launch_gui()
        return 0

    pnl_filter = "all" if args.all else args.pnl_filter

    result = run_pipeline(
        holdout_path=args.holdout,
        config_path=args.config,
        parquet_15m=args.parquet_15m,
        parquet_60m=args.parquet_60m,
        parquet_5m=args.parquet_5m,
        output_root=args.output_dir,
        scenario_index=args.scenario,
        pnl_filter=pnl_filter,
        reason_filter=args.reason_filter,
        date_start=args.date_start,
        date_end=args.date_end,
        pre_bars=args.pre_bars,
        post_bars=args.post_bars,
        warmup_bars=args.warmup_bars,
        allow_inferred=args.allow_inferred_prices,
        run_tag=args.run_tag,
    )

    if result["errors"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
