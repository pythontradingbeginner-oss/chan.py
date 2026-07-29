"""信号生命周期管理 —— 追踪 signal_key 从 CANDIDATE → CONFIRMED → INVALIDATED。

SignalLifecycleTracker:
  - 批量收集 SignalEvent, 按 signal_key 分组, 构建 revision 链
  - 检测状态异常 (如 revoked 信号重新出现)
  - 产出可用于研究的 final-state 视图 (每个 signal_key 的最新状态)

SignalJournal:
  - 将 SignalEvent/SignalAssessment/SignalDecision 持久化为 Parquet/CSV
  - 支持增量追加 (不重写整个文件)
  - 自动做列类型标准化
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .models import (
    ScoreGrade,
    SignalAssessment,
    SignalDecision,
    SignalDirection,
    SignalEvent,
    SignalRelation,
    SignalState,
)


# ═══════════════════════════════════════════
# SignalLifecycleTracker
# ═══════════════════════════════════════════

@dataclass
class LifecycleEntry:
    """一个 signal_key 的完整生命周期链。"""
    signal_key:    str
    revisions:     list[SignalEvent]   # revision 递增排序
    first_seen:    datetime
    last_seen:     datetime
    final_state:   SignalState
    num_revisions: int
    went_invalid:  bool            # 是否经历过 INVALIDATED

    @property
    def confirmed_revision(self) -> SignalEvent | None:
        """返回第一个 CONFIRMED revision, 若不存在则返回 None。"""
        for ev in self.revisions:
            if ev.state == SignalState.CONFIRMED:
                return ev
        return None


class SignalLifecycleTracker:
    """收集 SignalEvent 并按 signal_key 分组构建生命周期链。

    用法:
        tracker = SignalLifecycleTracker()
        for bar in bars:
            for event in extractor.extract_all(...):
                tracker.add(event)
        for entry in tracker.entries():
            print(f"{entry.signal_key}: candidate at {entry.first_seen}, "
                  f"final={entry.final_state}")
    """

    def __init__(self) -> None:
        self._groups: dict[str, list[SignalEvent]] = {}

    def add(self, event: SignalEvent) -> None:
        self._groups.setdefault(event.signal_key, []).append(event)

    def entries(self) -> list[LifecycleEntry]:
        result: list[LifecycleEntry] = []
        for key, events in self._groups.items():
            sorted_events = sorted(events, key=lambda e: e.revision)
            states = [e.state for e in sorted_events]
            result.append(LifecycleEntry(
                signal_key=key,
                revisions=sorted_events,
                first_seen=sorted_events[0].bar_end_time,
                last_seen=sorted_events[-1].bar_end_time,
                final_state=states[-1],
                num_revisions=len(sorted_events),
                went_invalid=SignalState.INVALIDATED in states,
            ))
        return result

    def to_dataframe(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for entry in self.entries():
            conf = entry.confirmed_revision
            rows.append({
                "signal_key":      entry.signal_key,
                "num_revisions":   entry.num_revisions,
                "first_seen":      entry.first_seen,
                "last_seen":       entry.last_seen,
                "final_state":     entry.final_state.value,
                "went_invalid":    entry.went_invalid,
                "confirmed_at":    conf.bar_end_time if conf else None,
                "final_primary":   entry.revisions[-1].primary_bsp if entry.revisions else "",
                "final_direction": entry.revisions[-1].direction.value if entry.revisions else "",
                "final_ref_price": entry.revisions[-1].reference_price if entry.revisions else 0.0,
            })
        return pd.DataFrame(rows)

    def __len__(self) -> int:
        return len(self._groups)


# ═══════════════════════════════════════════
# SignalJournal — Parquet/CSV 持久化
# ═══════════════════════════════════════════

_SIGNAL_DTYPES: dict[str, str] = {
    "event_id":              "str",
    "signal_key":            "str",
    "revision":              "int32",
    "symbol":                "str",
    "contract":              "str",
    "timeframe":             "str",
    "state":                 "str",
    "direction":             "str",
    "primary_bsp":           "str",
    "reference_price":       "float64",
    "bi_idx":                "int32",
    "structural_score":      "float32",
    "grade":                 "str",
}


class SignalJournal:
    """将信号规范化产物落盘为 CSV/Parquet, 支持增量追加。

    用法:
        journal = SignalJournal(reports_dir, format="csv")
        journal.write_events(events)
        journal.write_assessments(assessments)
        journal.write_decisions(decisions)
    """

    def __init__(self, reports_dir: str | Path, *, format: str = "csv") -> None:
        self._dir = Path(reports_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._format = format
        self._ext = ".parquet" if format == "parquet" else ".csv"

    # ── write ───────────────────────────────

    def write_events(self, events: list[SignalEvent]) -> None:
        self._write("signal_events", [e.to_dict() for e in events],
                     ["event_id", "signal_key", "state", "primary_bsp"])

    def write_assessments(self, assessments: list[SignalAssessment]) -> None:
        self._write("signal_assessments", [a.to_dict() for a in assessments],
                     ["assessment_id", "event_id", "grade", "scorer_id"])

    def write_decisions(self, decisions: list[SignalDecision]) -> None:
        self._write("signal_decisions", [d.to_dict() for d in decisions],
                     ["decision_id", "event_id", "accepted", "policy_id"])

    # ── read ─────────────────────────────────

    def read_events(self) -> pd.DataFrame:
        return self._read("signal_events")

    def read_assessments(self) -> pd.DataFrame:
        return self._read("signal_assessments")

    def read_decisions(self) -> pd.DataFrame:
        return self._read("signal_decisions")

    def list_files(self) -> list[Path]:
        return sorted(self._dir.glob(f"*{self._ext}"))

    # ── internal ────────────────────────────

    def _path(self, stem: str) -> Path:
        return self._dir / f"{stem}{self._ext}"

    def _write(
        self,
        stem: str,
        rows: list[dict[str, Any]],
        sort_cols: list[str],
    ) -> None:
        if not rows:
            return
        df_new = pd.DataFrame(rows)
        path = self._path(stem)
        if path.exists():
            df_old = self._read(stem)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            df_combined = df_combined.drop_duplicates(subset=sort_cols, keep="last")
            df_combined = df_combined.sort_values(sort_cols).reset_index(drop=True)
        else:
            df_combined = df_new.sort_values(sort_cols).reset_index(drop=True)
        self._save(df_combined, path)

    def _read(self, stem: str) -> pd.DataFrame:
        path = self._path(stem)
        if not path.exists():
            return pd.DataFrame()
        if self._format == "parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path)

    def _save(self, df: pd.DataFrame, path: Path) -> None:
        # 列类型标准化
        for col, dtype in _SIGNAL_DTYPES.items():
            if col in df.columns and dtype.startswith("float"):
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(dtype)
            elif col in df.columns and dtype == "int32":
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(dtype)

        if self._format == "parquet":
            df.to_parquet(path, index=False)
        else:
            df.to_csv(path, index=False, encoding="utf-8-sig")
