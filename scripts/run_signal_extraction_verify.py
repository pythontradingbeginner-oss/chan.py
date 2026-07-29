"""信号提取验证脚本 — 最小落地路径第 3 步。

目的:
  在已有回测管线中插入 SignalExtractor, 收集所有 SignalEvent + SignalAssessment,
  落盘为 CSV 供画图比对 (验证 bi_idx/state/reference_price)。

用法:
  python scripts/run_signal_extraction_verify.py --limit 2000 --reports-dir reports/signal_verify_01

输出:
  - reports/signal_verify_01/signal_events.csv
  - reports/signal_verify_01/signal_assessments.csv
  - reports/signal_verify_01/signal_summary.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from chan_futures.backtest import ChanBacktestConfig
from chan_futures.feed import prepare_ohlc_frame, row_to_klu
from Common.CEnum import KL_TYPE
from data_foundation import load_continuous_1m
from signal_core import SignalExtractor, SignalEvent
from signal_scoring import assess_event


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract chan.py BSP signals and score them.")
    p.add_argument(
        "--source-path", type=Path,
        default=Path("data/processed/RB_15m_continuous_raw.parquet"),
        help="Pre-aggregated 15-minute parquet (faster) or 1-minute.",
    )
    p.add_argument("--reports-dir", type=Path, default=Path("reports/signal_verify_01"))
    p.add_argument("--limit", type=int, default=2000, help="Bars to replay.")
    p.add_argument("--kl-type", type=str, default="K_15M",
                   choices=["K_1M", "K_5M", "K_15M", "K_30M", "K_60M"])
    p.add_argument("--fee-points", type=float, default=1.0)
    p.add_argument("--slippage-points", type=float, default=1.0)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.reports_dir.mkdir(parents=True, exist_ok=True)

    # ── 加载数据 ──
    kl_type = getattr(KL_TYPE, args.kl_type)
    if args.source_path.exists():
        frame = pd.read_parquet(args.source_path)
        frame = prepare_ohlc_frame(frame)
    else:
        print(f"falling back to 1m continuous → aggregate ...")
        frame_1m = load_continuous_1m()
        frame = prepare_ohlc_frame(frame_1m)
    if args.limit > 0:
        frame = frame.iloc[: args.limit].reset_index(drop=True)

    timeframe_str = args.kl_type.replace("K_", "").replace("M", "m")
    print(f"loaded {len(frame)} bars | timeframe={timeframe_str}")

    # ── 初始化提取器 ──
    extractor = SignalExtractor(
        symbol="RB",
        timeframe=timeframe_str,
        contract="RB_MAIN",
    )

    # ── 初始化 CChan ──
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_SRC

    chan = CChan(
        code="RB_MAIN",
        begin_time=None, end_time=None,
        data_src=DATA_SRC.CSV,
        lv_list=[kl_type],
        config=CChanConfig({
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
        }),
        autype=AUTYPE.NONE,
    )

    # ── 逐 bar 回放 + 提取信号 ──
    all_events: list[dict] = []
    all_assessments: list[dict] = []
    bar_records: list[dict] = []
    event_count = 0
    confirmed_count = 0
    grade_counts: dict[str, int] = {}

    for row_idx, (_, row) in enumerate(frame.iterrows()):
        klu = row_to_klu(row, kl_type=kl_type)
        chan.trigger_load({kl_type: [klu]})

        price = float(row["close"])
        ts = row["datetime"]

        # 记录 bar 级别的统计
        bi_cnt = len(chan[0].bi_list) if len(chan[0]) > 0 else 0
        seg_cnt = len(chan[0].seg_list) if len(chan[0]) > 0 else 0
        zs_cnt = len(chan[0].zs_list) if len(chan[0]) > 0 else 0

        bar_records.append({
            "row_idx": row_idx,
            "datetime": ts,
            "close": price,
            "bi_cnt": bi_cnt,
            "seg_cnt": seg_cnt,
            "zs_cnt": zs_cnt,
        })

        # 提取所有 BSP 信号
        try:
            bsp_list = chan[0].bs_point_lst
        except Exception:
            continue
        for bsp in bsp_list.bsp_iter():
            event = extractor.extract(bsp, chan=chan, bar_end_time=ts, lv_idx=0)
            if event is None:
                continue

            event_count += 1
            if event.state.value == "confirmed":
                confirmed_count += 1

            assessment = assess_event(event)
            grade_counts[assessment.grade.value] = grade_counts.get(assessment.grade.value, 0) + 1

            all_events.append(event.to_dict())
            all_assessments.append(assessment.to_dict())

    # ── 打印摘要 ──
    n_bars = len(frame)
    min_t = str(frame["datetime"].iloc[0])[:19]
    max_t = str(frame["datetime"].iloc[-1])[:19]
    print(f"\n{'='*60}")
    print(f"Signal Extraction Verification Summary")
    print(f"{'='*60}")
    print(f"  bars processed:        {n_bars}")
    print(f"  range:                  {min_t} → {max_t}")
    print(f"  total signal events:    {event_count}")
    print(f"  confirmed events:       {confirmed_count}")
    print(f"  candidate events:       {event_count - confirmed_count}")
    print(f"  events per bar avg:     {event_count / max(n_bars, 1):.2f}")
    print(f"  grade distribution:")
    for g in ("ideal", "standard", "weak"):
        cnt = grade_counts.get(g, 0)
        pct = cnt / max(event_count, 1) * 100
        print(f"    {g:<10s}: {cnt:4d} ({pct:5.1f}%)")
    print(f"{'='*60}")

    # ── 落盘 ──
    df_events = pd.DataFrame(all_events)
    df_assess = pd.DataFrame(all_assessments)
    df_bars = pd.DataFrame(bar_records)

    events_path = args.reports_dir / "signal_events.csv"
    assess_path = args.reports_dir / "signal_assessments.csv"
    bars_path = args.reports_dir / "signal_bars.csv"
    summary_path = args.reports_dir / "signal_summary.csv"

    df_events.to_csv(events_path, index=False, encoding="utf-8-sig")
    df_assess.to_csv(assess_path, index=False, encoding="utf-8-sig")
    df_bars.to_csv(bars_path, index=False, encoding="utf-8-sig")

    grade_summary = [
        {"metric": f"grade_{g}", "value": grade_counts.get(g, 0)}
        for g in ("ideal", "standard", "weak")
    ]
    summary_rows = [
        {"metric": "bars", "value": n_bars},
        {"metric": "total_events", "value": event_count},
        {"metric": "confirmed_events", "value": confirmed_count},
        {"metric": "candidate_events", "value": event_count - confirmed_count},
        {"metric": "events_per_bar", "value": round(event_count / max(n_bars, 1), 2)},
        *grade_summary,
        {"metric": "range_start", "value": min_t},
        {"metric": "range_end", "value": max_t},
    ]
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False, encoding="utf-8-sig")

    print(f"\nsaved:")
    print(f"  {events_path}")
    print(f"  {assess_path}")
    print(f"  {bars_path}")
    print(f"  {summary_path}")

    # ── 样本展示 ──
    if all_events:
        print(f"\n--- Sample SignalEvent (first 3) ---")
        for i, evt in enumerate(all_events[:3]):
            print(
                f"  [{i}] sig={evt['signal_key']} | state={evt['state']} | "
                f"bsp={evt['primary_bsp']} | bi_idx={evt['bi_idx']} | "
                f"ref_price={evt['reference_price']:.1f}"
            )
        print(f"\n--- Sample SignalEvent (last 3) ---")
        for i, evt in enumerate(all_events[-3:]):
            print(
                f"  [{len(all_events)-3+i}] sig={evt['signal_key']} | state={evt['state']} | "
                f"bsp={evt['primary_bsp']} | bi_idx={evt['bi_idx']} | "
                f"ref_price={evt['reference_price']:.1f}"
            )

    if all_assessments:
        print(f"\n--- Sample SignalAssessment (first 5) ---")
        for i, a in enumerate(all_assessments[:5]):
            print(
                f"  [{i}] grade={a['grade']} | structural={a['structural_score']} | "
                f"blockers={a['hard_blockers']} | scorer={a['scorer_id']}"
            )


if __name__ == "__main__":
    main()
