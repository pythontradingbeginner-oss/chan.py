"""Build the P0 Qingpai rule-review samples from real RB 15m bars.

The replay is deliberately point-in-time.  Every structure snapshot is frozen at
``decision_time``; bars after that time are included only as a shaded teaching
outcome and are marked ``is_visible_at_decision=false`` in the fixture CSV.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
import pandas as pd

from Chan import CChan
from Common.CEnum import AUTYPE, DATA_SRC
from chan_futures.feed import dataframe_to_klu_iter
from chan_futures.rb_chan_plot import (
    DEFAULT_DATA_PATH,
    RB_15M_KL_TYPE,
    load_rb_15m_frame,
    make_rb_15m_chan_config,
    prepare_rb_15m_frame,
)
from signal_core import SignalExtractor
from signal_scoring import assess_event


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "qingpai_p0"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "qingpai_p0"
RULE_CONTRACT_PATH = PROJECT_ROOT / "configs" / "qingpai_rule_contract_v1.json"
EXPECTED_DATA_SHA256 = "0FA531E3E5531985F1E9BBDBF118E5BE9596FF97396CECF57D52805A528CA068"


@dataclass(frozen=True)
class SampleSpec:
    sample_id: str
    slug: str
    title: str
    decision_time: str
    rule_ids: tuple[str, ...]
    observation: str
    target_bi_idx: int | None = None
    target_bsp: str | None = None
    direction: str | None = None
    pre_bars: int = 110
    post_bars: int = 18
    special: str | None = None


SAMPLE_SPECS = (
    SampleSpec(
        sample_id="P0-RB15-001",
        slug="bullish_center_t3_buy",
        title="上涨进入中枢与三买",
        decision_time="2018-06-01 13:45:00",
        target_bi_idx=26,
        target_bsp="3a",
        direction="long",
        rule_ids=("QP-STRUCT-002", "QP-STRUCT-003", "QP-BSP-001"),
        observation="进入笔向上，中枢偏多；三买回踩极值保持在 ZG 上方。",
        pre_bars=175,
    ),
    SampleSpec(
        sample_id="P0-RB15-002",
        slug="bearish_center_t3_sell",
        title="下跌进入中枢与三卖",
        decision_time="2018-08-28 10:45:00",
        target_bi_idx=77,
        target_bsp="3a",
        direction="short",
        rule_ids=("QP-STRUCT-002", "QP-STRUCT-003", "QP-BSP-001"),
        observation="进入笔向下，中枢偏空；三卖反弹极值保持在 ZD 下方。",
    ),
    SampleSpec(
        sample_id="P0-RB15-003",
        slug="t2_sell_dual_stop",
        title="二卖的双层失效位",
        decision_time="2018-05-07 13:45:00",
        target_bi_idx=7,
        target_bsp="2",
        direction="short",
        rule_ids=("QP-BSP-002", "QP-RISK-001"),
        observation="关联一卖高点管理整套 setup，二卖反弹高点管理本次订单止损。",
        pre_bars=95,
    ),
    SampleSpec(
        sample_id="P0-RB15-004",
        slug="t1_no_divergence_blocked",
        title="无背驰的一买必须硬阻断",
        decision_time="2018-08-27 11:00:00",
        target_bi_idx=76,
        target_bsp="1",
        direction="long",
        rule_ids=("QP-BSP-003", "QP-MACD-001", "QP-MACD-002"),
        observation="当前评分可达到 STANDARD，但 no_divergence 必须优先拒绝。",
    ),
    SampleSpec(
        sample_id="P0-RB15-005",
        slug="t1_divergence_comparison",
        title="一卖背驰与面积峰值对照",
        decision_time="2018-05-31 15:00:00",
        target_bi_idx=25,
        target_bsp="1",
        direction="short",
        rule_ids=("QP-MACD-001", "QP-MACD-002", "QP-MACD-003"),
        observation="面积是主比较量，峰值和价格推进只作补充裁决。",
        pre_bars=120,
    ),
    SampleSpec(
        sample_id="P0-RB15-006",
        slug="t3b_wrong_side_blocked",
        title="三卖回到中枢必须拒绝",
        decision_time="2021-10-20 21:15:00",
        target_bi_idx=769,
        target_bsp="3b",
        direction="short",
        rule_ids=("QP-BSP-001", "QP-BSP-003"),
        observation="反弹极值没有保持在 ZD 下方，当前 scorer 正确给出 not_below_zs。",
        pre_bars=165,
    ),
    SampleSpec(
        sample_id="P0-RB15-007",
        slug="t3b_close_false_pass",
        title="三买不能用确认收盘价代替回踩极值",
        decision_time="2024-09-26 13:45:00",
        target_bi_idx=1560,
        target_bsp="3b",
        direction="long",
        rule_ids=("QP-BSP-001", "QP-BSP-003"),
        observation="确认时收盘价在 ZG 上方，但三买回踩笔低点进入中枢；P1 应按结构极值阻断。",
        pre_bars=250,
    ),
    SampleSpec(
        sample_id="P0-RB15-008",
        slug="rollover_raw_gap",
        title="主力换月原始价差不能继承结构",
        decision_time="2018-11-30 21:15:00",
        rule_ids=("QP-RB-001",),
        observation="RB1901 切换 RB1905 产生约 337 点原始缺口，直接连续会制造虚假笔和中枢。",
        special="rollover",
        pre_bars=45,
        post_bars=30,
    ),
    SampleSpec(
        sample_id="P0-RB15-009",
        slug="overnight_gap_stop",
        title="隔夜跳空越过止损的执行",
        decision_time="2020-02-03 09:15:00",
        rule_ids=("QP-RISK-004", "QP-RB-002"),
        observation="春节后首个可成交价较前收低约 267 点，保护止损不能等待后续 K 线确认。",
        special="overnight_gap",
        pre_bars=35,
        post_bars=35,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build point-in-time Qingpai P0 RB samples.")
    parser.add_argument("--data-path", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--skip-hash-check", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.fixture_dir.mkdir(parents=True, exist_ok=True)

    data_hash = _sha256(args.data_path)
    if not args.skip_hash_check and data_hash != EXPECTED_DATA_SHA256:
        raise ValueError(
            "RB 15m source fingerprint changed: "
            f"expected {EXPECTED_DATA_SHA256}, got {data_hash}"
        )

    contract = json.loads(RULE_CONTRACT_PATH.read_text(encoding="utf-8"))
    valid_rule_ids = {rule["id"] for rule in contract["rules"]}
    unknown = sorted({rule_id for spec in SAMPLE_SPECS for rule_id in spec.rule_ids} - valid_rule_ids)
    if unknown:
        raise ValueError(f"sample specs reference unknown rule ids: {unknown}")

    frame = prepare_rb_15m_frame(load_rb_15m_frame(args.data_path))
    frame = _add_macd(frame)
    index_by_time = {pd.Timestamp(value): idx for idx, value in enumerate(frame["datetime"])}
    missing = [spec.decision_time for spec in SAMPLE_SPECS if pd.Timestamp(spec.decision_time) not in index_by_time]
    if missing:
        raise ValueError(f"decision times missing from RB data: {missing}")

    snapshots = _replay_snapshots(frame)
    manifest_samples: list[dict[str, Any]] = []
    fixture_windows: list[pd.DataFrame] = []
    image_paths: list[Path] = []

    for spec in SAMPLE_SPECS:
        decision_idx = index_by_time[pd.Timestamp(spec.decision_time)]
        start_idx = max(0, decision_idx - spec.pre_bars)
        end_idx = min(len(frame) - 1, decision_idx + spec.post_bars)
        window = frame.iloc[start_idx : end_idx + 1].copy().reset_index(drop=False)
        window = window.rename(columns={"index": "source_row_idx"})
        window["sample_id"] = spec.sample_id
        window["is_visible_at_decision"] = window["datetime"] <= pd.Timestamp(spec.decision_time)
        fixture_windows.append(
            window[
                [
                    "sample_id",
                    "source_row_idx",
                    "datetime",
                    "trading_day",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "open_interest",
                    "active_symbol",
                    "flags",
                    "is_visible_at_decision",
                ]
            ]
        )

        snapshot = snapshots[spec.sample_id]
        relevant_center = _select_relevant_center(snapshot, spec)
        levels = _derive_levels(snapshot, relevant_center, spec)
        image_path = args.output_dir / f"{spec.sample_id}_{spec.slug}.png"
        _plot_sample(window, snapshot, relevant_center, levels, spec, image_path)
        image_paths.append(image_path)

        manifest_samples.append(
            {
                **asdict(spec),
                "data_start": _iso(window.iloc[0]["datetime"]),
                "data_end": _iso(window.iloc[-1]["datetime"]),
                "visible_through": _iso(spec.decision_time),
                "image": str(image_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "target_event": snapshot.get("target_event"),
                "target_assessment": snapshot.get("target_assessment"),
                "target_bsp_snapshot": snapshot.get("target_bsp_snapshot"),
                "relevant_center": relevant_center,
                "derived_levels": levels,
            }
        )

    fixture_frame = pd.concat(fixture_windows, ignore_index=True)
    fixture_frame.to_csv(
        args.fixture_dir / "rb_15m_windows.csv", index=False, encoding="utf-8-sig"
    )

    manifest = {
        "fixture_version": "1.0.0-p0",
        "generated_from_commit": _git_head(),
        "data_source": str(args.data_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "data_sha256": data_hash,
        "point_in_time_contract": (
            "Only rows with is_visible_at_decision=true and the frozen snapshot may drive a decision."
        ),
        "samples": manifest_samples,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (args.fixture_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (args.output_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    _write_fixture_readme(args.fixture_dir)
    _write_report_index(args.output_dir, manifest_samples)
    _plot_overview(image_paths, args.output_dir / "P0_RB15_overview.png")

    print(f"built {len(SAMPLE_SPECS)} P0 samples")
    print(args.output_dir / "P0_RB15_overview.png")
    print(args.fixture_dir / "manifest.json")
    print(args.fixture_dir / "rb_15m_windows.csv")


def _replay_snapshots(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    targets_by_time = {pd.Timestamp(spec.decision_time): spec for spec in SAMPLE_SPECS}
    latest_target = max(targets_by_time)
    snapshots: dict[str, dict[str, Any]] = {}
    chan = CChan(
        code="RB",
        begin_time=None,
        end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[RB_15M_KL_TYPE],
        config=make_rb_15m_chan_config(),
        autype=AUTYPE.NONE,
    )

    feed_frame = frame[frame["datetime"] <= latest_target]
    for row, klu in zip(
        feed_frame.itertuples(index=False),
        dataframe_to_klu_iter(feed_frame, kl_type=RB_15M_KL_TYPE),
    ):
        chan.trigger_load({RB_15M_KL_TYPE: [klu]})
        ts = pd.Timestamp(row.datetime)
        spec = targets_by_time.get(ts)
        if spec is None:
            continue
        snapshots[spec.sample_id] = _freeze_snapshot(chan, spec, ts)

    missing = sorted({spec.sample_id for spec in SAMPLE_SPECS} - snapshots.keys())
    if missing:
        raise RuntimeError(f"failed to capture snapshots: {missing}")
    return snapshots


def _freeze_snapshot(chan: CChan, spec: SampleSpec, decision_time: pd.Timestamp) -> dict[str, Any]:
    kl = chan[RB_15M_KL_TYPE]
    bis = [_freeze_bi(bi) for bi in kl.bi_list]
    centers = [_freeze_center(idx, zs) for idx, zs in enumerate(kl.zs_list)]
    bsps = [_freeze_bsp(bsp) for bsp in kl.bs_point_lst.bsp_iter()]
    target_bsp = _find_target_bsp(kl, spec)

    target_event = None
    target_assessment = None
    target_bsp_snapshot = None
    if target_bsp is not None:
        target_bsp_snapshot = _freeze_bsp(target_bsp)
        extractor = SignalExtractor(
            symbol="RB", timeframe="15m", contract="RB_MAIN", data_run_id="qingpai_p0"
        )
        event = extractor.extract(
            target_bsp, chan=chan, bar_end_time=decision_time, lv_idx=0
        )
        if event is not None:
            target_event = event.to_dict()
            assessment = assess_event(event)
            target_assessment = assessment.to_dict()

    return {
        "decision_time": _iso(decision_time),
        "bar_count": len(kl.lst),
        "bi_count": len(bis),
        "zs_count": len(centers),
        "bsp_count": len(bsps),
        "bis": bis,
        "centers": centers,
        "bsps": bsps,
        "target_event": target_event,
        "target_assessment": target_assessment,
        "target_bsp_snapshot": target_bsp_snapshot,
    }


def _find_target_bsp(kl: Any, spec: SampleSpec) -> Any | None:
    if spec.target_bi_idx is None:
        return None
    matches = []
    for bsp in kl.bs_point_lst.bsp_iter():
        types = set(bsp.type2str().split(","))
        direction = "long" if bsp.is_buy else "short"
        if (
            bsp.bi.idx == spec.target_bi_idx
            and spec.target_bsp in types
            and direction == spec.direction
        ):
            matches.append(bsp)
    if len(matches) != 1:
        raise ValueError(
            f"{spec.sample_id}: expected one BSP bi={spec.target_bi_idx} "
            f"type={spec.target_bsp} direction={spec.direction}, got {len(matches)}"
        )
    return matches[0]


def _freeze_bi(bi: Any) -> dict[str, Any]:
    return {
        "idx": int(bi.idx),
        "direction": bi.dir.name.lower(),
        "is_sure": bool(bi.is_sure),
        "begin_time": _chan_time(bi.get_begin_klu().time),
        "end_time": _chan_time(bi.get_end_klu().time),
        "begin_price": float(bi.get_begin_val()),
        "end_price": float(bi.get_end_val()),
        "low": float(bi._low()),
        "high": float(bi._high()),
        "macd_area": float(bi.Cal_MACD_area()),
        "macd_peak": float(bi.Cal_MACD_peak()),
        "price_slope": float(bi.Cal_MACD_slope()),
        "price_amp": float(bi.Cal_MACD_amp()),
    }


def _freeze_center(idx: int, zs: Any) -> dict[str, Any]:
    bi_in = zs.bi_in
    direction = None
    start_price = None
    entry_begin_time = None
    entry_end_time = None
    if bi_in is not None:
        direction = "bullish" if bi_in.is_up() else "bearish"
        start_price = float(bi_in.get_begin_val())
        entry_begin_time = _chan_time(bi_in.get_begin_klu().time)
        entry_end_time = _chan_time(bi_in.get_end_klu().time)
    return {
        "idx": int(idx),
        "is_sure": bool(zs.is_sure),
        "begin_bi_idx": int(zs.begin_bi.idx),
        "end_bi_idx": int(zs.end_bi.idx),
        "begin_time": _chan_time(zs.begin.time),
        "end_time": _chan_time(zs.end.time),
        "zd": float(zs.low),
        "zg": float(zs.high),
        "entry_direction": direction,
        "start_price": start_price,
        "entry_begin_time": entry_begin_time,
        "entry_end_time": entry_end_time,
    }


def _freeze_bsp(bsp: Any) -> dict[str, Any]:
    related = bsp.relate_bsp1
    return {
        "bi_idx": int(bsp.bi.idx),
        "types": bsp.type2str().split(","),
        "direction": "long" if bsp.is_buy else "short",
        "is_sure": bool(bsp.bi.is_sure),
        "signal_time": _chan_time(bsp.klu.time),
        "structural_price": float(bsp.bi.get_end_val()),
        "reference_close": float(bsp.klu.close),
        "related_bsp1_bi_idx": None if related is None else int(related.bi.idx),
        "related_bsp1_price": None if related is None else float(related.bi.get_end_val()),
    }


def _select_relevant_center(
    snapshot: dict[str, Any], spec: SampleSpec
) -> dict[str, Any] | None:
    centers = snapshot["centers"]
    target = snapshot.get("target_bsp_snapshot")
    event = snapshot.get("target_event")
    if not centers:
        return None

    if event and event.get("zs_high") is not None and event.get("zs_low") is not None:
        matching = [
            center
            for center in centers
            if math.isclose(center["zg"], float(event["zs_high"]))
            and math.isclose(center["zd"], float(event["zs_low"]))
        ]
        if matching:
            return matching[-1]

    if target is not None and spec.target_bsp in {"3a", "3b"}:
        bi = next(item for item in snapshot["bis"] if item["idx"] == spec.target_bi_idx)
        prior = [center for center in centers if center["end_bi_idx"] < bi["idx"]]
        if spec.direction == "long":
            valid = [center for center in prior if bi["low"] > center["zg"]]
        else:
            valid = [center for center in prior if bi["high"] < center["zd"]]
        if valid:
            return valid[-1]
        if prior:
            return prior[-1]

    if target is not None:
        prior = [center for center in centers if center["end_bi_idx"] < target["bi_idx"]]
        if prior:
            return prior[-1]
    return centers[-1]


def _derive_levels(
    snapshot: dict[str, Any], center: dict[str, Any] | None, spec: SampleSpec
) -> dict[str, float | None]:
    target = snapshot.get("target_bsp_snapshot")
    if target is None:
        return {"setup_invalidation": None, "execution_stop": None}

    setup = None
    execution = float(target["structural_price"])
    if spec.target_bsp in {"2", "2s"}:
        setup = target.get("related_bsp1_price")
    elif spec.target_bsp in {"3a", "3b"} and center is not None:
        setup = center["zg"] if spec.direction == "long" else center["zd"]
    elif spec.target_bsp in {"1", "1p"}:
        setup = execution
    return {
        "setup_invalidation": None if setup is None else float(setup),
        "execution_stop": execution,
    }


def _plot_sample(
    window: pd.DataFrame,
    snapshot: dict[str, Any],
    center: dict[str, Any] | None,
    levels: dict[str, float | None],
    spec: SampleSpec,
    output_path: Path,
) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, (ax, ax_macd) = plt.subplots(
        2,
        1,
        figsize=(18, 9),
        dpi=180,
        sharex=True,
        gridspec_kw={"height_ratios": [3.6, 1.25], "hspace": 0.06},
    )
    x = np.arange(len(window))
    time_to_x = {pd.Timestamp(value): idx for idx, value in enumerate(window["datetime"])}
    decision_time = pd.Timestamp(spec.decision_time)
    decision_x = time_to_x[decision_time]

    _draw_candles(ax, window)
    ax.axvspan(decision_x + 0.5, len(window) - 0.5, color="#d9d9d9", alpha=0.32)
    if spec.post_bars > 0:
        ax.text(
            min(decision_x + 1, len(window) - 1),
            0.98,
            "outcome only",
            transform=ax.get_xaxis_transform(),
            va="top",
            fontsize=8,
            color="#666666",
        )
    ax.axvline(decision_x, color="#111111", linestyle="--", linewidth=1.1, label="decision cutoff")

    for bi in snapshot["bis"]:
        begin_x = _lookup_x(time_to_x, bi["begin_time"])
        end_x = _lookup_x(time_to_x, bi["end_time"])
        if begin_x is None or end_x is None:
            continue
        color = "#c00000" if bi["direction"] == "up" else "#006b3c"
        ax.plot(
            [begin_x, end_x],
            [bi["begin_price"], bi["end_price"]],
            color=color,
            linewidth=1.45,
            alpha=0.86 if bi["is_sure"] else 0.45,
            zorder=4,
        )

    for item in snapshot["centers"]:
        begin_x = _lookup_x(time_to_x, item["begin_time"])
        end_x = _lookup_x(time_to_x, item["end_time"])
        if begin_x is None and end_x is None:
            continue
        x0 = 0 if begin_x is None else begin_x
        x1 = decision_x if end_x is None else min(end_x, decision_x)
        if x1 < 0 or x0 >= len(window):
            continue
        is_relevant = center is not None and item["idx"] == center["idx"]
        face = "#f2c94c" if is_relevant else "#7fb3d5"
        rect = patches.Rectangle(
            (x0, item["zd"]),
            max(x1 - x0, 0.8),
            item["zg"] - item["zd"],
            facecolor=face,
            edgecolor="#8a6d00" if is_relevant else "#2f75b5",
            linewidth=1.2 if is_relevant else 0.8,
            alpha=0.23,
            zorder=1,
        )
        ax.add_patch(rect)
        ax.text(x0, item["zg"], f"ZS{item['idx']}", fontsize=7, va="bottom")

    if center is not None:
        _annotate_center(ax, time_to_x, decision_x, center)

    target = snapshot.get("target_bsp_snapshot")
    if target is not None:
        target_x = _lookup_x(time_to_x, target["signal_time"])
        if target_x is not None:
            marker = "^" if target["direction"] == "long" else "v"
            color = "#c00000" if target["direction"] == "long" else "#006b3c"
            ax.scatter(
                [target_x],
                [target["structural_price"]],
                marker=marker,
                s=115,
                color=color,
                edgecolors="#111111",
                linewidths=0.7,
                zorder=8,
                label=f"BSP {spec.target_bsp} structural extreme",
            )
            if not math.isclose(target["reference_close"], target["structural_price"]):
                ax.scatter(
                    [decision_x],
                    [target["reference_close"]],
                    marker="*",
                    s=95,
                    color="#7f3c8d",
                    zorder=8,
                    label="confirmation close",
                )

    _draw_level(ax, levels.get("setup_invalidation"), "setup invalidation", "#b22222", "--")
    _draw_level(ax, levels.get("execution_stop"), "execution stop", "#f28e2b", ":")
    _annotate_special(ax, window, spec, decision_x)

    hist_colors = np.where(window["macd_hist"] >= 0, "#c00000", "#009e60")
    ax_macd.bar(x, window["macd_hist"], color=hist_colors, alpha=0.45, width=0.78)
    ax_macd.plot(x, window["macd_dif"], color="#2f5597", linewidth=0.9, label="DIF")
    ax_macd.plot(x, window["macd_dea"], color="#f28e2b", linewidth=0.9, label="DEA")
    ax_macd.axhline(0, color="#666666", linewidth=0.7)
    ax_macd.axvline(decision_x, color="#111111", linestyle="--", linewidth=1.0)
    ax_macd.axvspan(decision_x + 0.5, len(window) - 0.5, color="#d9d9d9", alpha=0.32)

    assessment = snapshot.get("target_assessment") or {}
    blocker_text = assessment.get("hard_blockers") or "none"
    score = assessment.get("structural_score")
    score_text = "n/a" if score is None else f"{float(score):.2f}"
    detail = (
        f"{spec.sample_id} | rules: {', '.join(spec.rule_ids)}\n"
        f"{spec.observation}\n"
        f"current scorer: score={score_text}, blockers={blocker_text}"
    )
    ax.text(
        0.012,
        0.985,
        detail,
        transform=ax.transAxes,
        va="top",
        fontsize=8.5,
        bbox=dict(boxstyle="round,pad=0.35", facecolor="white", alpha=0.88, edgecolor="#888888"),
        zorder=10,
    )

    tick_step = max(1, len(window) // 11)
    ticks = list(range(0, len(window), tick_step))
    if ticks[-1] != len(window) - 1:
        ticks.append(len(window) - 1)
    ax_macd.set_xticks(ticks)
    ax_macd.set_xticklabels(
        [pd.Timestamp(window.iloc[idx]["datetime"]).strftime("%m-%d\n%H:%M") for idx in ticks],
        fontsize=8,
    )
    ax.set_title(f"{spec.sample_id}  {spec.title}", fontsize=13)
    ax.set_ylabel("RB price")
    ax_macd.set_ylabel("MACD")
    ax_macd.set_xlabel("RB 15m bars")
    for axis in (ax, ax_macd):
        axis.grid(True, alpha=0.18)
    ax.legend(loc="lower left", fontsize=7.5, ncol=3)
    ax_macd.legend(loc="upper left", fontsize=7.5, ncol=2)
    fig.subplots_adjust(left=0.055, right=0.985, top=0.94, bottom=0.09, hspace=0.06)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def _draw_candles(ax: Any, window: pd.DataFrame) -> None:
    for x, row in enumerate(window.itertuples(index=False)):
        up = row.close >= row.open
        color = "#c00000" if up else "#009e60"
        ax.vlines(x, row.low, row.high, color=color, linewidth=0.65, alpha=0.78, zorder=2)
        bottom = min(row.open, row.close)
        height = max(abs(row.close - row.open), 0.5)
        ax.add_patch(
            patches.Rectangle(
                (x - 0.31, bottom),
                0.62,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.45,
                alpha=0.68,
                zorder=3,
            )
        )


def _annotate_center(
    ax: Any, time_to_x: dict[pd.Timestamp, int], decision_x: int, center: dict[str, Any]
) -> None:
    entry_start_x = _lookup_x(time_to_x, center["entry_begin_time"])
    entry_end_x = _lookup_x(time_to_x, center["entry_end_time"])
    if entry_start_x is not None and entry_end_x is not None:
        ax.annotate(
            "",
            xy=(entry_end_x, center["zg"] if center["entry_direction"] == "bullish" else center["zd"]),
            xytext=(entry_start_x, center["start_price"]),
            arrowprops=dict(arrowstyle="->", color="#7f6000", linewidth=1.4),
        )
        label_x = (entry_start_x + entry_end_x) / 2
        label_y = (
            center["start_price"]
            + (center["zg"] if center["entry_direction"] == "bullish" else center["zd"])
        ) / 2
        ax.text(
            label_x,
            label_y,
            f"entry {center['entry_direction']}",
            fontsize=8,
            color="#7f6000",
            rotation=18 if center["entry_direction"] == "bullish" else -18,
            ha="center",
            va="bottom",
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.62, pad=1.0),
            zorder=7,
        )
    if center["start_price"] is not None:
        ax.hlines(
            center["start_price"],
            0 if entry_start_x is None else entry_start_x,
            decision_x,
            color="#7f6000",
            linestyle="-.",
            linewidth=1.0,
            label="center start",
        )
    ax.axhline(center["zg"], color="#8a6d00", linewidth=0.85, alpha=0.72, label="ZG")
    ax.axhline(center["zd"], color="#8a6d00", linewidth=0.85, alpha=0.72, label="ZD")


def _draw_level(ax: Any, value: float | None, label: str, color: str, style: str) -> None:
    if value is None or not math.isfinite(value):
        return
    ax.axhline(value, color=color, linestyle=style, linewidth=1.15, alpha=0.9, label=f"{label} {value:.0f}")


def _annotate_special(ax: Any, window: pd.DataFrame, spec: SampleSpec, decision_x: int) -> None:
    if spec.special not in {"rollover", "overnight_gap"} or decision_x <= 0:
        return
    row = window.iloc[decision_x]
    prev = window.iloc[decision_x - 1]
    gap = float(row["open"] - prev["close"])
    ax.annotate(
        f"gap {gap:+.0f}\n{prev['active_symbol']} -> {row['active_symbol']}",
        xy=(decision_x, row["open"]),
        xytext=(max(0, decision_x - 16), (row["open"] + prev["close"]) / 2),
        arrowprops=dict(arrowstyle="->", color="#7030a0", linewidth=1.4),
        fontsize=9,
        color="#7030a0",
        bbox=dict(facecolor="white", edgecolor="#7030a0", alpha=0.82),
    )


def _add_macd(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    close = result["close"].astype(float)
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    result["macd_dif"] = dif
    result["macd_dea"] = dea
    result["macd_hist"] = 2.0 * (dif - dea)
    return result


def _plot_overview(image_paths: list[Path], output_path: Path) -> None:
    fig, axes = plt.subplots(3, 3, figsize=(18, 11), dpi=130)
    for ax, image_path in zip(axes.flat, image_paths):
        ax.imshow(plt.imread(image_path))
        ax.set_title(image_path.stem.split("_", 1)[0], fontsize=9)
        ax.axis("off")
    for ax in axes.flat[len(image_paths) :]:
        ax.axis("off")
    fig.suptitle("Qingpai P0 - RB 15m labeled samples", fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def _write_report_index(output_dir: Path, samples: list[dict[str, Any]]) -> None:
    lines = [
        "# Qingpai P0 RB 15m Samples",
        "",
        "The grey area in each chart is outcome-only and must never drive the frozen decision.",
        "",
        "![overview](P0_RB15_overview.png)",
        "",
    ]
    for sample in samples:
        image_name = Path(sample["image"]).name
        assessment = sample.get("target_assessment") or {}
        lines.extend(
            [
                f"## {sample['sample_id']} {sample['title']}",
                "",
                sample["observation"],
                "",
                f"- Decision time: `{sample['decision_time']}`",
                f"- Rules: `{', '.join(sample['rule_ids'])}`",
                f"- Current hard blockers: `{assessment.get('hard_blockers') or 'none'}`",
                "",
                f"![{sample['sample_id']}]({image_name})",
                "",
            ]
        )
    (output_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_fixture_readme(fixture_dir: Path) -> None:
    text = """# Qingpai P0 RB 15m fixtures

These fixtures are generated by `scripts/build_qingpai_p0_samples.py` from the
local RB 15m parquet whose SHA256 is recorded in `manifest.json`.

`rb_15m_windows.csv` may contain bars after a sample's decision cutoff for visual
teaching.  Only rows where `is_visible_at_decision=true` may be consumed by a
strategy or point-in-time assertion.  The structure dictionaries in
`manifest.json` were frozen during an incremental `CChan.trigger_load()` replay.
"""
    (fixture_dir / "README.md").write_text(text, encoding="utf-8")


def _lookup_x(time_to_x: dict[pd.Timestamp, int], value: Any) -> int | None:
    if value is None:
        return None
    return time_to_x.get(pd.Timestamp(value))


def _chan_time(value: Any) -> str:
    return _iso(str(value).replace("/", "-"))


def _iso(value: Any) -> str:
    return pd.Timestamp(value).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _git_head() -> str:
    import subprocess

    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


if __name__ == "__main__":
    main()
