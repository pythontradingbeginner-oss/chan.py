"""CChan 快照管理器 —— 处理多级别 bar 完成对齐。

遵循 strategy_demo3.py 的模式:
  - 最小级别 bar 到达时，合成上级别 bar
  - 当上级别 bar 完整时 (所有子 bar 到齐)，保存快照
  - 在上级别 bar 不完整期间，从快照深拷贝后 trigger_load 当前帧

这是防止未来函数的关键：在上级别 bar 尚未完成期间，
后续子 bar 到达时，必须回退到上一个完整快照重新计算，
否则 CChan 会把尚未完成的上级别 bar 当作"已经确定的 K 线"。

用法:
    mgr = ChanSnapshotManager(chan=CChan(...), lv_list=[KL_TYPE.K_60M, KL_TYPE.K_15M])
    for klu_15m in data_source:
        chan = mgr.feed(klu_15m)  # 返回当前帧 CChan
        # 在 chan 上运行策略
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from Chan import CChan
from Common.CEnum import DATA_FIELD, KL_TYPE
from KLine.KLine_Unit import CKLine_Unit


@dataclass
class _LevelBuffer:
    """单个级别的 bar 组装缓冲。"""

    kl_type: KL_TYPE
    bars_per_level: int  # 几根子 bar 合成一根此级别的 bar
    pending: list[CKLine_Unit] = field(default_factory=list)

    def feed(self, klu: CKLine_Unit) -> CKLine_Unit | None:
        """喂入子级别 bar, 返回 None 或合成完毕的上级别 bar。"""
        self.pending.append(klu)
        if len(self.pending) >= self.bars_per_level:
            synthesized = _synthesize_bar(self.pending)
            self.pending.clear()
            return synthesized
        return None


class ChanSnapshotManager:
    """管理 CChan 的快照生命周期。

    只处理两个级别 (e.g. K_15M + K_60M)。单级别场景直接 trigger_load。
    """

    def __init__(
        self,
        chan: CChan,
        lv_list: list[KL_TYPE],
        *,
        parent_minutes: int | None = None,
        child_minutes: int = 15,
    ) -> None:
        """初始化快照管理器。

        Args:
            chan: 已初始化 (trigger_step=True) 的 CChan 实例
            lv_list: 级别列表，由大到小
            parent_minutes: 父级别分钟数。None 则从 lv_list 推断
            child_minutes: 子级别分钟数
        """
        self.lv_list = lv_list

        # ── 多级别模式 ──
        if len(lv_list) >= 2:
            parent_kl = lv_list[0]
            child_kl = lv_list[1]
            if parent_minutes is None:
                parent_minutes = _kl_type_minutes(parent_kl)
            bars_per = max(1, parent_minutes // child_minutes)
            self._buffer = _LevelBuffer(kl_type=parent_kl, bars_per_level=bars_per)
            self._child_lv = child_kl
            self._parent_lv = parent_kl
            self._multi_level = bars_per > 1
        else:
            self._multi_level = False
            self._child_lv = lv_list[0]
            self._parent_lv = lv_list[0]
            self._buffer = _LevelBuffer(kl_type=lv_list[0], bars_per_level=1)

        self._snapshot: CChan | None = copy.deepcopy(chan)
        self._current: CChan | None = chan
        self._child_bars: list[CKLine_Unit] = []

    # ── 核心接口 ──

    @property
    def current(self) -> CChan | None:
        """返回当前帧的 CChan (策略应该用这个)。"""
        return self._current

    @property
    def is_multi_level(self) -> bool:
        return self._multi_level

    def feed(self, klu: CKLine_Unit) -> CChan | None:
        """喂入一根子级别 bar, 返回当前帧 CChan。

        Returns:
            当前帧 CChan, 或 None (如果快照状态错误)。
        """
        self._child_bars.append(klu)

        if not self._multi_level:
            # ── 单级别: 直接 trigger_load ──
            if self._current is not None:
                self._current.trigger_load({self._child_lv: [klu]})
            return self._current

        # ── 多级别: 检查父级别 bar 是否完成 ──
        parent_bar = self._buffer.feed(klu)

        if parent_bar is not None:
            # 父级别 bar 完成 → 保存快照
            self._current = copy.deepcopy(self._snapshot)
            self._current.trigger_load({
                self._parent_lv: [parent_bar],
                self._child_lv: list(self._child_bars),
            })
            self._snapshot = copy.deepcopy(self._current)
            self._child_bars.clear()
        else:
            # 父级别 bar 未完成 → 从快照恢复，用当前累积的 child bars 重新计算
            if self._snapshot is not None:
                partial_klu = _synthesize_bar(self._buffer.pending) if self._buffer.pending else None
                self._current = copy.deepcopy(self._snapshot)
                load_dict: dict[KL_TYPE, list[CKLine_Unit]] = {self._child_lv: list(self._child_bars)}
                if partial_klu is not None:
                    load_dict[self._parent_lv] = [partial_klu]
                self._current.trigger_load(load_dict)

        return self._current

    def full_snapshot(self) -> CChan | None:
        """返回上一个完整快照 (不含当前未完成 bar)。"""
        return self._snapshot

    def save_snapshot(self, path: str) -> None:
        """将快照序列化到磁盘。"""
        if self._snapshot is not None:
            self._snapshot.chan_dump_pickle(path)

    def load_snapshot(self, path: str) -> None:
        """从磁盘加载快照。"""
        self._snapshot = CChan.chan_load_pickle(path)
        self._current = copy.deepcopy(self._snapshot)


# ════════════════════════════════════════════════════════════════
# 辅助
# ════════════════════════════════════════════════════════════════


def _synthesize_bar(child_bars: list[CKLine_Unit]) -> CKLine_Unit:
    """从子级别 bar 列表合成上级别 bar。"""
    if len(child_bars) == 1:
        return child_bars[0]
    item = {
        DATA_FIELD.FIELD_TIME: child_bars[-1].time,
        DATA_FIELD.FIELD_OPEN: child_bars[0].open,
        DATA_FIELD.FIELD_CLOSE: child_bars[-1].close,
        DATA_FIELD.FIELD_HIGH: max(klu.high for klu in child_bars),
        DATA_FIELD.FIELD_LOW: min(klu.low for klu in child_bars),
    }
    klu = CKLine_Unit(item, autofix=True)
    klu.kl_type = child_bars[0].kl_type
    return klu


def _kl_type_minutes(kl_type: KL_TYPE) -> int:
    """将 KL_TYPE 转为分钟数。"""
    mapping = {
        KL_TYPE.K_1M: 1, KL_TYPE.K_5M: 5, KL_TYPE.K_15M: 15,
        KL_TYPE.K_30M: 30, KL_TYPE.K_60M: 60, KL_TYPE.K_DAY: 1440,
    }
    return mapping.get(kl_type, 15)
