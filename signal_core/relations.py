"""多级别信号关系管理 —— 构建和维护 SignalRelation。

职责:
  1. 从 SignalExtractor 产出的 parent_event_id 构建 SignalRelation
  2. 验证时间诚实性 (parent.available_at ≤ child.available_at)
  3. 建立双向索引: child → parents, parent → children
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd

from .models import SignalEvent, SignalRelation


class RelationBuilder:
    """从 SignalEvent 列表构建多级别关联关系。

    用法:
        builder = RelationBuilder()
        for event in events:
            builder.add_event(event)
        relations = builder.build()
        for rel in relations:
            if rel.relation_type == "resonance":
                print(f"resonance: {rel.child_event_id} ← {rel.parent_event_id}")
    """

    def __init__(self) -> None:
        self._events: dict[str, SignalEvent] = {}      # event_id → event
        self._by_signal_key: dict[str, list[str]] = defaultdict(list)  # sig_key → [event_ids]

    def add_event(self, event: SignalEvent) -> None:
        self._events[event.event_id] = event
        self._by_signal_key[event.signal_key].append(event.event_id)

    def build(self) -> list[SignalRelation]:
        relations: list[SignalRelation] = []

        for event in self._events.values():
            if event.parent_event_id is None:
                continue

            parent = self._find_latest_revision(event.parent_event_id)
            if parent is None:
                continue

            # ── 时间诚实性检查 ──
            if parent.available_at <= event.available_at:
                relation_type = "resonance"
            else:
                # 上级别信号在子级别决策时尚未可见 → 标记为潜在未来函数
                relation_type = "future_leak"

            relations.append(SignalRelation(
                child_event_id=event.event_id,
                parent_event_id=parent.event_id,
                relation_type=relation_type,
                known_at=max(parent.available_at, event.available_at),
            ))

        return relations

    def _find_latest_revision(self, parent_signal_key_or_event_id: str) -> SignalEvent | None:
        """查找上级别信号的最新 revision。"""
        # 先尝试作为 event_id 查找
        if parent_signal_key_or_event_id in self._events:
            evt = self._events[parent_signal_key_or_event_id]
            # 找同 signal_key 下最大 revision
            event_ids = self._by_signal_key.get(evt.signal_key, [])
            if not event_ids:
                return evt
            latest_id = max(event_ids, key=lambda eid: self._events[eid].revision)
            return self._events.get(latest_id)
        # 尝试作为 signal_key 查找
        event_ids = self._by_signal_key.get(parent_signal_key_or_event_id, [])
        if event_ids:
            latest_id = max(event_ids, key=lambda eid: self._events[eid].revision)
            return self._events.get(latest_id)
        return None

    def to_dataframe(self, relations: list[SignalRelation]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for r in relations:
            rows.append({
                "child_event_id":  r.child_event_id,
                "parent_event_id": r.parent_event_id,
                "relation_type":   r.relation_type,
                "known_at":        r.known_at,
            })
        return pd.DataFrame(rows)


def check_time_honesty(relations: list[SignalRelation]) -> tuple[int, int]:
    """统计关系中的未来函数泄漏数量。

    Returns:
        (total_relations, future_leak_count)
    """
    total = len(relations)
    leaks = sum(1 for r in relations if r.relation_type == "future_leak")
    return total, leaks
