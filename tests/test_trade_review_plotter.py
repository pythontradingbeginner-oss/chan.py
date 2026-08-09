"""测试 scripts/trade_review_plotter.py —— 交易复盘图表 V2。

覆盖 Codex 12 条整改中的核心要求：
  1. 坐标与 datetime 精确对应（entry/exit 索引 → holdout 时间断言）
  2. 真实 fill（signal/fill 价 + 双边滑点 + 手续费，gross-fees-slip=net）
  3. 场景成本（scenario 倍率应用到 execution config）
  4. 配置一致性（CChan 用 config.chan.to_dict()，含 macd_algo=full_area）
  5. 时间缺失时失败（find_bar_index 抛错，fail-closed）
  6. 禁止静默降级（匹配失败默认抛错，allow_inferred 才退化为 close 推断）
  7. 筛选保持全局编号（orig_index 在筛选后保留）
  8. manifest 来源字段（price_source、config/data sha256、git_head、marker datetime）
  9. entry/exit/context 三个独立截止状态
  10. 11 张复合图片非空且包含三个面板
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.trade_review_plotter import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_DATA_15M,
    DEFAULT_DATA_60M,
    DEFAULT_DATA_5M,
    DEFAULT_HOLDOUT,
    DEFAULT_OOS_START,
    TradeReviewError,
    _backtest_audit_pnl,
    _chan_cutoff_audit,
    _inclusive_end_ts,
    _lookup_decision_trace,
    _lookup_fills,
    _naive_ts,
    _safe_float,
    _sha256,
    apply_scenario_execution,
    assign_original_index,
    build_display_chan,
    build_replay_window,
    compute_trade_window,
    enrich_trades,
    find_bar_index,
    make_trade_filename,
    run_pipeline,
    run_replay_backtest,
    scenario_name,
    select_scenario,
)


@pytest.fixture(scope="module")
def frames():
    """加载三级别数据 + 构造 replay 窗口（与 run_p7_holdout 一致）。"""
    frame_15m = pd.read_parquet(DEFAULT_DATA_15M)
    frame_60m = pd.read_parquet(DEFAULT_DATA_60M)
    frame_5m = pd.read_parquet(DEFAULT_DATA_5M)
    for f in (frame_15m, frame_60m, frame_5m):
        dt = pd.to_datetime(f["datetime"])
        if dt.dt.tz is not None:
            dt = dt.dt.tz_localize(None)
        f["datetime"] = dt
        f.sort_values("datetime", inplace=True)
        f.drop_duplicates("datetime", inplace=True)
        f.reset_index(drop=True, inplace=True)
    return frame_15m, frame_60m, frame_5m


@pytest.fixture(scope="module")
def holdout_data():
    with open(DEFAULT_HOLDOUT, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def holdout_trades(holdout_data):
    trades, meta = select_scenario(holdout_data, 0)
    return assign_original_index(trades), meta


@pytest.fixture(scope="module")
def replay(frames):
    f15 = frames[0]
    return build_replay_window(
        f15, oos_start=DEFAULT_OOS_START, warmup_bars=2000,
        configured_warmup_bars=800,
    )


@pytest.fixture(scope="module")
def backtest_result(frames, replay):
    """跑一次严格窗口回测（scenario 0，fee/slip=1.0x），全部测试共用。"""
    from chan_futures.config_loader import load_config

    config = load_config(DEFAULT_CONFIG)
    r, result = run_replay_backtest(
        config,
        frame_15m=frames[0],
        frame_60m=frames[1],
        frame_5m=frames[2],
        oos_start=DEFAULT_OOS_START,
        warmup_bars=2000,
        fee_mult=1.0,
        slip_mult=1.0,
    )
    assert len(result.trades) == 11, "回测应产出 11 笔交易（与 holdout 一致）"
    return result


# ════════════════════════════════════════════════════════════════════
# 1. 坐标与 datetime 精确对应
# ════════════════════════════════════════════════════════════════════


def test_coord_datetime_exact_match(replay, holdout_trades):
    trades, _ = holdout_trades
    for t in trades[:3]:
        entry_idx = find_bar_index(replay, t["entry_time"], "entry")
        exit_idx = find_bar_index(replay, t["exit_time"], "exit")
        assert str(replay.iloc[entry_idx]["datetime"])[:19] == t["entry_time"][:19]
        assert str(replay.iloc[exit_idx]["datetime"])[:19] == t["exit_time"][:19]


def test_coord_matches_backtest_entry_bar(replay, holdout_trades, backtest_result):
    """回测 entry_bar 就是 replay 行号，且其 datetime 与 holdout 完全相等。"""
    trades, _ = holdout_trades
    bt_by_key = {
        (str(bt["entry_time"])[:19], str(bt["exit_time"])[:19]): bt
        for bt in backtest_result.trades
    }
    for t in trades:
        bt = bt_by_key[(t["entry_time"][:19], t["exit_time"][:19])]
        entry_row = replay.iloc[bt["entry_bar"]]
        exit_row = replay.iloc[bt["exit_bar"]]
        assert str(entry_row["datetime"])[:19] == t["entry_time"][:19]
        assert str(exit_row["datetime"])[:19] == t["exit_time"][:19]


# ════════════════════════════════════════════════════════════════════
# 5. 时间缺失时失败
# ════════════════════════════════════════════════════════════════════


def test_missing_time_fails(replay):
    with pytest.raises(TradeReviewError):
        find_bar_index(replay, "2026-07-16 10:05:00", "entry")  # 不存在的时间点


def test_date_only_end_is_inclusive():
    assert _inclusive_end_ts("2026-07-16") > pd.Timestamp("2026-07-16 23:59:59")
    assert _inclusive_end_ts("2026-07-16 10:00:00") == pd.Timestamp(
        "2026-07-16 10:00:00"
    )


# ════════════════════════════════════════════════════════════════════
# 2. 真实 fill + 成本校验
# ════════════════════════════════════════════════════════════════════


def test_real_fills_and_cost(holdout_trades, backtest_result, replay):
    trades, meta = holdout_trades
    config = _config()
    fee_points = config.execution.fee_points * 1.0
    slip_points = config.execution.slippage_points * 1.0
    enriched = enrich_trades(
        trades, backtest_result, fee_points=fee_points,
        slippage_points=slip_points, allow_inferred=False, replay=replay,
    )
    assert len(enriched) == 11
    for t in enriched:
        assert t["price_source"] == "backtest"
        assert t["replay_match_check"] == "ok"
        assert t["decision_trace_check"] == "ok"
        assert t["cost_check"] == "ok"
        gross = _safe_float(t.get("gross_pnl_points"))
        fees = _safe_float(t.get("fees_points"))
        slip = _safe_float(t.get("slippage_points"))
        net = _safe_float(t.get("net_pnl_points"))
        assert abs(gross - fees - slip - net) < 0.01
        # 真实 fill 价必须存在且为正
        assert _safe_float(t.get("entry_fill_price")) > 0
        assert _safe_float(t.get("exit_fill_price")) > 0
        # 滑点 = fill - signal（取绝对值）
        assert abs(
            abs(_safe_float(t.get("exit_fill_price")) - _safe_float(t.get("exit_signal_price")))
            - _safe_float(t.get("exit_slippage_points"))
        ) < 0.01
        assert _safe_float(t["backtest_pnl_points"]) == pytest.approx(net)
        assert _safe_float(t["computed_fees_points"]) == pytest.approx(fees)
        assert _safe_float(t["computed_slippage_points"]) == pytest.approx(slip)


def test_cost_audit_rejects_replay_drift():
    holdout = {
        "gross_pnl_points": -6.0,
        "fees_points": 2.0,
        "slippage_points": 2.0,
        "net_pnl_points": -10.0,
    }
    replay_trade = {"pnl_points": -9.0}
    fills = {
        "entry_fee_points": 1.0,
        "exit_fee_points": 1.0,
        "entry_slippage_points": 1.0,
        "exit_slippage_points": 1.0,
    }
    ok, detail = _backtest_audit_pnl(holdout, replay_trade, fills)
    assert not ok
    assert "replay_net" in detail


def test_lookup_fills_signal_vs_fill(backtest_result, holdout_trades):
    trades, _ = holdout_trades
    t = trades[0]
    fills = _lookup_fills(backtest_result, t["entry_time"], t["exit_time"])
    assert fills["entry_signal_price"] is not None
    assert fills["entry_fill_price"] is not None
    assert fills["exit_fill_price"] is not None
    assert abs(fills["entry_fill_price"] - fills["entry_signal_price"]) == fills["entry_slippage_points"]


# ════════════════════════════════════════════════════════════════════
# 3. 场景成本
# ════════════════════════════════════════════════════════════════════


def test_scenario_costs(holdout_data):
    for i in range(3):
        trades, meta = select_scenario(holdout_data, i)
        assert meta["fee_multiplier"] == (1.0, 1.5, 2.0)[i]
        assert meta["slippage_multiplier"] == (1.0, 1.5, 2.0)[i]
        assert len(trades) == 11
    with pytest.raises(TradeReviewError):
        select_scenario(holdout_data, -1)


def test_apply_scenario_execution():
    from chan_futures.config_loader import load_config

    config = load_config(DEFAULT_CONFIG)
    base_fee = config.execution.fee_points
    base_slip = config.execution.slippage_points
    for mult in (1.0, 1.5, 2.0):
        cfg2 = apply_scenario_execution(config, mult, mult)
        assert cfg2.execution.fee_points == pytest.approx(base_fee * mult)
        assert cfg2.execution.slippage_points == pytest.approx(base_slip * mult)
        # 其余字段保持不变
        assert cfg2.chan.to_dict() == config.chan.to_dict()
        assert cfg2.execution.contract_multiplier == config.execution.contract_multiplier
        assert cfg2.execution.price_tick == config.execution.price_tick


def test_scenario_backtest_fee_consistency(frames, backtest_result):
    """scenario 1/2 的回测成本应随倍率放大。"""
    from chan_futures.config_loader import load_config

    config = load_config(DEFAULT_CONFIG)
    _, result2 = run_replay_backtest(
        config,
        frame_15m=frames[0], frame_60m=frames[1], frame_5m=frames[2],
        oos_start=DEFAULT_OOS_START, warmup_bars=2000,
        fee_mult=2.0, slip_mult=2.0,
    )
    assert result2.config.execution.fee_points == pytest.approx(config.execution.fee_points * 2.0)
    assert result2.config.execution.slippage_points == pytest.approx(config.execution.slippage_points * 2.0)
    # 2x 场景的净收益应 <= 1x（成本更高）
    net1 = sum(float(t["pnl_points"]) for t in backtest_result.trades)
    net2 = sum(float(t["pnl_points"]) for t in result2.trades)
    assert net2 <= net1


# ════════════════════════════════════════════════════════════════════
# 4. 配置一致性（真实策略 chan 配置）
# ════════════════════════════════════════════════════════════════════


def _config():
    from chan_futures.config_loader import load_config

    return load_config(DEFAULT_CONFIG)


def test_chan_config_matches_strategy(backtest_result):
    """CChan 必须用 config.chan.to_dict()（macd_algo=full_area），非 rb_chan_plot 默认 peak。"""
    config = backtest_result.config
    chan_dict = config.chan.to_dict()
    assert chan_dict["macd_algo"] == "full_area"
    assert chan_dict["trigger_step"] is True
    assert chan_dict["bi_strict"] is True


def test_build_display_chan_uses_real_config(replay):
    config = _config()
    chan = build_display_chan(replay, config.chan.to_dict(), 2800)
    assert len(chan[0]) > 0
    # klu.idx == 行号
    n = 0
    for klc in chan[0].lst:
        for klu in klc.lst:
            assert klu.idx == n
            n += 1
    assert n == 2801  # 0..2800


def test_entry_exit_context_snapshots_have_exact_cutoffs(
    replay, holdout_trades,
):
    config = _config()
    trade = holdout_trades[0][0]
    win = compute_trade_window(
        replay, trade["entry_time"], trade["exit_time"], pre_bars=120, post_bars=60
    )
    cutoffs = {
        "entry": win["entry_idx"],
        "exit": win["exit_idx"],
        "context": win["plot_end"],
    }
    audits = {}
    for name, cutoff in cutoffs.items():
        chan = build_display_chan(replay, config.chan.to_dict(), cutoff)
        audits[name] = _chan_cutoff_audit(chan, replay, cutoff)
        assert audits[name]["last_klu_index"] == cutoff
        assert audits[name]["klu_count"] == cutoff + 1
    assert audits["entry"]["cutoff_datetime"] == trade["entry_time"][:19]
    assert audits["exit"]["cutoff_datetime"] == trade["exit_time"][:19]
    assert audits["context"]["cutoff_index"] > audits["exit"]["cutoff_index"]


# ════════════════════════════════════════════════════════════════════
# 6. 禁止静默降级
# ════════════════════════════════════════════════════════════════════


def test_no_silent_degradation_default(holdout_trades, backtest_result, replay):
    """默认情况下，匹配失败应抛错而非静默推断。"""
    trades, meta = holdout_trades
    config = _config()
    # 篡改一笔交易的 exit_time 使其无法与回测匹配
    bad = [dict(t) for t in trades]
    bad[0]["exit_time"] = "2099-01-01 10:00:00"
    with pytest.raises(TradeReviewError):
        enrich_trades(
            bad, backtest_result,
            fee_points=config.execution.fee_points,
            slippage_points=config.execution.slippage_points,
            allow_inferred=False, replay=replay,
        )


def test_exact_trade_set_rejects_missing_holdout_trade(
    holdout_trades, backtest_result, replay,
):
    trades, _ = holdout_trades
    config = _config()
    with pytest.raises(TradeReviewError, match="交易集合不完全一致"):
        enrich_trades(
            trades[:-1],
            backtest_result,
            fee_points=config.execution.fee_points,
            slippage_points=config.execution.slippage_points,
            allow_inferred=False,
            replay=replay,
        )


def test_allow_inferred_watermark_flag(holdout_trades, backtest_result, replay):
    """显式 allow_inferred 时返回 close 推断价格并标注 price_source。"""
    trades, meta = holdout_trades
    config = _config()
    bad = [dict(t) for t in trades]
    # 改成「时间合法但与任何回测交易都不匹配」的一笔（不会触发回测）
    bad[0]["exit_time"] = "2026-07-16 11:00:00"  # 存在于 replay，但无对应 backtest 交易
    enriched = enrich_trades(
        bad, backtest_result,
        fee_points=config.execution.fee_points,
        slippage_points=config.execution.slippage_points,
        allow_inferred=True, replay=replay,
    )
    # 第一笔被推断，其余仍为 backtest
    inferred = [t for t in enriched if t.get("price_source") == "close_inferred"]
    assert len(inferred) == 1
    # 被推断的交易价格来自 close，且带推断标记
    bad_trade = bad[0]
    entry_idx = find_bar_index(replay, bad_trade["entry_time"], "entry")
    assert enriched[0]["entry_price"] == _safe_float(replay.iloc[entry_idx]["close"])
    assert enriched[0]["cost_check"] == "N/A"


# ════════════════════════════════════════════════════════════════════
# 7. 筛选保持全局编号
# ════════════════════════════════════════════════════════════════════


def test_filter_preserves_original_index():
    trades = [
        {"entry_time": "2026-07-16 10:00:00", "exit_reason": "structure_stop",
         "net_pnl_points": -10.0},
        {"entry_time": "2026-07-16 14:45:00", "exit_reason": "opposite_signal_flat",
         "net_pnl_points": 7.0},
        {"entry_time": "2026-07-22 14:30:00", "exit_reason": "opposite_signal_flat",
         "net_pnl_points": 15.0},
    ]
    assigned = assign_original_index(trades)
    # 筛选盈利交易
    wins = [t for t in assigned if _safe_float(t.get("net_pnl_points")) > 0]
    assert [t["_orig_index"] for t in wins] == [2, 3]
    # 文件名保留原始序号
    assert make_trade_filename(wins[0], wins[0]["_orig_index"]).startswith("W02_")
    assert make_trade_filename(wins[1], wins[1]["_orig_index"]).startswith("W03_")


# ════════════════════════════════════════════════════════════════════
# decision_trace 字段
# ════════════════════════════════════════════════════════════════════


def test_decision_trace_fields(backtest_result, holdout_trades, replay):
    trades, _ = holdout_trades
    t = trades[0]
    trace = _lookup_decision_trace(backtest_result, t["entry_time"], str(t["direction"]))
    assert trace, "应能从 decision_trace 找到入场记录"
    for field in (
        "event_id", "signal_key", "policy_id", "bsp_type", "direction",
        "setup_invalidation_price", "execution_stop_price",
    ):
        assert field in trace
    assert "parent_direction" in trace or "multi_level_accepted" in trace


# ════════════════════════════════════════════════════════════════════
# 8. manifest 来源字段
# ════════════════════════════════════════════════════════════════════


def _flatten_result_manifest(trade: dict, orig_index: int, meta: dict) -> dict:
    from scripts.trade_review_plotter import _flatten_trade_row

    result = {
        "index": orig_index,
        "orig_index": orig_index,
        "filename": make_trade_filename(trade, orig_index),
        "status": "ok",
        "trade": trade,
        "marker_entry_datetime": trade.get("entry_time"),
        "marker_exit_datetime": trade.get("exit_time"),
        "marker_dt_match": "ok",
    }
    return _flatten_trade_row(result, meta)


def test_manifest_source_fields(backtest_result, holdout_trades, replay):
    trades, _ = holdout_trades
    config = _config()
    enriched = enrich_trades(
        trades, backtest_result,
        fee_points=config.execution.fee_points,
        slippage_points=config.execution.slippage_points,
        allow_inferred=False, replay=replay,
    )
    meta = {
        "config_sha256": _sha256(DEFAULT_CONFIG),
        "data_sha256": {
            "15m": _sha256(DEFAULT_DATA_15M),
            "60m": _sha256(DEFAULT_DATA_60M),
            "5m": _sha256(DEFAULT_DATA_5M),
        },
        "report_git_head": "abc123",
        "current_git_head": "def456",
        "scenario_index": 0,
        "scenario_name": scenario_name(0),
        "fee_multiplier": 1.0,
        "slippage_multiplier": 1.0,
    }
    row = _flatten_result_manifest(enriched[0], 1, meta)
    assert row["price_source"] == "backtest"
    assert row["config_sha256"] == _sha256(DEFAULT_CONFIG)
    assert row["data_15m_sha256"] == _sha256(DEFAULT_DATA_15M)
    assert row["data_60m_sha256"] == _sha256(DEFAULT_DATA_60M)
    assert row["data_5m_sha256"] == _sha256(DEFAULT_DATA_5M)
    assert row["report_git_head"] == "abc123"
    assert row["current_git_head"] == "def456"
    assert row["marker_dt_match"] == "ok"
    assert row["scenario_name"] == scenario_name(0)
    assert row["orig_index"] == 1


# ════════════════════════════════════════════════════════════════════
# 9. 端到端：11 张图片非空 + 验收
# ════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def pipeline_result(tmp_path_factory):
    """跑一次完整 pipeline（scenario 0），输出到临时目录。"""
    out_root = tmp_path_factory.mktemp("trade_review_v2_test")
    result = run_pipeline(
        holdout_path=DEFAULT_HOLDOUT,
        config_path=DEFAULT_CONFIG,
        parquet_15m=DEFAULT_DATA_15M,
        parquet_60m=DEFAULT_DATA_60M,
        parquet_5m=DEFAULT_DATA_5M,
        output_root=out_root,
        scenario_index=0,
        pnl_filter="all",
        allow_inferred=False,
        run_tag="test",
        warmup_bars=2000,
    )
    return result


def test_pipeline_e2e_acceptance(pipeline_result):
    """验收：11 笔、8 亏 3 盈、净 -48、price_source 全 backtest、marker 全匹配。"""
    r = pipeline_result
    assert r["ok"] == 11, f"应生成 11 张图，实际 {r['ok']}"
    assert r["errors"] == 0

    import csv
    out_dir = Path(r["output_dir"])
    manifest = out_dir / "trade_review_manifest.csv"
    assert manifest.exists()
    with manifest.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 11
    nets = [float(x["net_pnl_points"]) for x in rows]
    assert sum(nets) == -48.0
    wins = sum(1 for n in nets if n > 0)
    losses = sum(1 for n in nets if n <= 0)
    assert wins == 3 and losses == 8
    assert all(x["price_source"] == "backtest" for x in rows)
    assert all(x["marker_dt_match"] == "ok" for x in rows)
    assert all(x["cost_check"] == "ok" for x in rows)
    assert all(x["replay_match_check"] == "ok" for x in rows)
    assert all(x["decision_trace_check"] == "ok" for x in rows)
    assert all(x["snapshot_time_check"] == "ok" for x in rows)
    assert all(x["entry_snapshot_cutoff_datetime"] == x["entry_time"][:19] for x in rows)
    assert all(x["exit_snapshot_cutoff_datetime"] == x["exit_time"][:19] for x in rows)
    assert all(int(x["context_requested_post_exit_bars"]) == 60 for x in rows)
    assert all(
        int(x["context_post_exit_bars"])
        == int(x["context_snapshot_cutoff_index"]) - int(x["exit_snapshot_cutoff_index"])
        for x in rows
    )
    assert all(int(x["context_post_exit_bars"]) <= 60 for x in rows)
    truncated = [x for x in rows if x["context_truncated"] == "True"]
    assert [int(x["orig_index"]) for x in truncated] == [11]
    assert all(x["data_15m_sha256"] == _sha256(DEFAULT_DATA_15M) for x in rows)
    assert all(x["data_60m_sha256"] == _sha256(DEFAULT_DATA_60M) for x in rows)
    assert all(x["data_5m_sha256"] == _sha256(DEFAULT_DATA_5M) for x in rows)


def test_pipeline_e2e_pngs_nonempty(pipeline_result):
    from PIL import Image

    out_dir = Path(pipeline_result["output_dir"])
    pngs = sorted(out_dir.glob("*.png"))
    assert len(pngs) == 11
    for p in pngs:
        assert p.stat().st_size > 0, f"PNG 为空: {p.name}"
        with Image.open(p) as image:
            assert image.height > image.width, f"复合图未包含三个纵向面板: {p.name}"
            lo, hi = image.convert("L").getextrema()
            assert lo < 240 and hi > 245, f"PNG 像素近似空白: {p.name}"


def test_pipeline_e2e_orig_indices(pipeline_result):
    import csv
    out_dir = Path(pipeline_result["output_dir"])
    with (out_dir / "trade_review_manifest.csv").open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [int(r["orig_index"]) for r in rows] == list(range(1, 12))
    # 文件名编号与全局序号一致（W/L + 两位原序号，如 L01_、W03_）
    names = {Path(r["filename"]).name for r in rows}
    for i in range(1, 12):
        prefix = f"{i:02d}_"
        assert any(
            (n.startswith("L" + prefix) or n.startswith("W" + prefix)) for n in names
        ), f"缺少全局序号 {i} 的图"
