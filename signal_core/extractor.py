"""SignalExtractor: CBS_Point + CChan 上下文 → SignalEvent.

这是整个信号规范化架构中唯一直接接触 CBS_Point / CBi / CChan 的模块。
其余所有模块 (评分、决策、回测、出场) 只处理 SignalEvent 等纯 dataclass。

Feature Key 映射:
  CBS_Point.features 里的 key 由 chan.py 内部确定 (如 bsp2_retrace_rate),
  提取器负责把它们映射到 SignalEvent.features 中的标准化 key (如 retrace_rate),
  这样评分器不需要知道原始 key 名。

去重机制:
  - 内部追踪每个 BSP (bi_idx + direction + primary_bsp) 的最后已知状态
  - 仅在 BSP 首次出现或生命周期状态变更时产出新事件
  - 同一根 BSP 在后续 bar 中状态不变时, extract() 返回 None
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from Common.CEnum import BSP_TYPE

from .models import (
    SignalDirection,
    SignalEvent,
    SignalState,
    new_event_id,
    new_signal_key,
)


# ── CBS_Point.features 原始 key → 标准化 key ──
FEATURE_KEY_MAP: dict[str, str] = {
    "divergence_rate":       "divergence_rate",
    "zs_cnt":                "zs_cnt",
    "bsp1_bi_amp":           "bi_amp",
    "bsp2_retrace_rate":     "retrace_rate",
    "bsp2_break_bi_amp":     "break_bi_amp",
    "bsp2_bi_amp":           "bsp2_bi_amp",
    "bsp2s_retrace_rate":    "retrace_rate",
    "bsp2s_break_bi_amp":    "break_bi_amp",
    "bsp2s_bi_amp":          "bsp2s_bi_amp",
    "bsp2s_lv":              "t2s_level",
    "bsp3_zs_height":        "zs_height",
    "bsp3_bi_amp":           "bsp3_bi_amp",
    "bsp_bi_amp":            "bi_amp",
}


_BSP_TYPE_TO_STR: dict[BSP_TYPE, str] = {
    BSP_TYPE.T1:  "1",
    BSP_TYPE.T1P: "1p",
    BSP_TYPE.T2:  "2",
    BSP_TYPE.T2S: "2s",
    BSP_TYPE.T3A: "3a",
    BSP_TYPE.T3B: "3b",
}


class SignalExtractor:
    """将 CBS_Point 转换为 SignalEvent, 内置去重检测。

    用法:
        extractor = SignalExtractor(symbol="RB", timeframe="15m")
        for bsp in chan[lv_idx].bs_point_lst.bsp_iter():
            event = extractor.extract(bsp, chan=chan, bar_end_time=bar_time, lv_idx=0)
            if event is not None:
                events.append(event)
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        contract: str = "",
        chan_version: str = "v3_public",
        data_run_id: str = "",
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.contract = contract or f"{symbol}_MAIN"
        self.chan_version = chan_version
        self.data_run_id = data_run_id
        # 去重: (bi_idx, dir, primary_bsp) → (sig_key, last_state, revision)
        self._seen: dict[tuple[int, str, str], tuple[str, SignalState, int]] = {}

    def extract(
        self, bsp, *, chan, bar_end_time: datetime, lv_idx: int = 0,
    ) -> SignalEvent | None:
        """从 CBS_Point 提取一个 SignalEvent (带去重)。"""
        types = getattr(bsp, "type", None)
        if not types:
            return None

        primary = _bsp_primary_type(types)
        all_types = tuple(_bsp_type_list(types))
        direction = SignalDirection.LONG if bsp.is_buy else SignalDirection.SHORT
        current_state = _determine_state(bsp)

        dedup_key = (bsp.bi.idx, direction.value, primary)

        if dedup_key in self._seen:
            last_sig_key, last_state, last_rev = self._seen[dedup_key]
            if current_state == last_state:
                return None
            sig_key = last_sig_key
            rev = last_rev + 1
        else:
            sig_key = new_signal_key(
                self.contract, self.timeframe, bsp.bi.idx, direction, primary)
            rev = 0

        self._seen[dedup_key] = (sig_key, current_state, rev)

        ref_price = float(bsp.klu.close)
        bi_begin = _safe_bi_begin(bsp)
        zs_high, zs_low = _nearest_zs_range(bsp)
        features = _extract_features(bsp)
        try:
            features.setdefault("bi_amp", float(bsp.bi.amp()))
        except Exception:
            features.setdefault("bi_amp", None)

        parent_id = _find_parent_event_id(bsp, chan, lv_idx, self.contract)
        seg_idx: int | None = bsp.bi.seg_idx

        return SignalEvent(
            event_id=new_event_id(sig_key, rev),
            signal_key=sig_key,
            revision=rev,
            symbol=self.symbol,
            contract=self.contract,
            timeframe=self.timeframe,
            bar_end_time=bar_end_time,
            available_at=bar_end_time,
            state=current_state,
            direction=direction,
            primary_bsp=primary,
            bsp_types=all_types,
            reference_price=ref_price,
            bi_idx=bsp.bi.idx,
            seg_idx=seg_idx,
            bi_begin_price=float(bi_begin) if bi_begin is not None else None,
            zs_high=float(zs_high) if zs_high is not None else None,
            zs_low=float(zs_low) if zs_low is not None else None,
            parent_event_id=parent_id,
            feature_schema_version="v0",
            features=features,
            chan_version=self.chan_version,
            data_run_id=self.data_run_id,
        )

    def mark_invalidated(
        self, event_id: str, invalidation_time: datetime,
    ) -> SignalEvent | None:
        """生成一个 INVALIDATED 版本的 SignalEvent (去重不保护, 调用方自行控制)。"""
        parts = event_id.rsplit("_r", 1)
        if len(parts) != 2:
            return None
        sig_key = parts[0]
        dedup_match = None
        for dk, (sk, st, rev) in self._seen.items():
            if sk == sig_key:
                dedup_match = (dk, sk, st, rev)
                break
        if dedup_match is None:
            rev = 0
        else:
            rev = dedup_match[3] + 1
            self._seen[dedup_match[0]] = (sig_key, SignalState.INVALIDATED, rev)
        return SignalEvent(
            event_id=new_event_id(sig_key, rev),
            signal_key=sig_key,
            revision=rev,
            symbol=self.symbol,
            contract=self.contract,
            timeframe=self.timeframe,
            bar_end_time=invalidation_time,
            available_at=invalidation_time,
            state=SignalState.INVALIDATED,
            direction=SignalDirection.LONG,
            primary_bsp="",
            bsp_types=(),
            reference_price=0.0,
            bi_idx=-1,
            seg_idx=None,
            bi_begin_price=None,
            zs_high=None,
            zs_low=None,
            parent_event_id=None,
            feature_schema_version="v0",
            features={},
            chan_version=self.chan_version,
            data_run_id=self.data_run_id,
        )


# ═══════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════

def _bsp_primary_type(types: list) -> str:
    first = types[0]
    if isinstance(first, BSP_TYPE):
        return _BSP_TYPE_TO_STR.get(first, str(first))
    return str(first)


def _bsp_type_list(types: list) -> list[str]:
    result: list[str] = []
    for t in types:
        if isinstance(t, BSP_TYPE):
            result.append(_BSP_TYPE_TO_STR.get(t, str(t)))
        else:
            result.append(str(t))
    return result


def _determine_state(bsp) -> SignalState:
    bi_sure = getattr(bsp.bi, "is_sure", False)
    return SignalState.CONFIRMED if bi_sure else SignalState.CANDIDATE


def _safe_bi_begin(bsp) -> float | None:
    try:
        return float(bsp.bi.get_begin_val())
    except Exception:
        return None


def _nearest_zs_range(bsp) -> tuple[float | None, float | None]:
    seg = getattr(bsp.bi, "parent_seg", None)
    if seg is None:
        return None, None
    zs_lst = getattr(seg, "zs_lst", [])
    if not zs_lst:
        return None, None
    for zs in reversed(zs_lst):
        if hasattr(zs, "is_one_bi_zs") and not zs.is_one_bi_zs():
            return float(zs.high), float(zs.low)
    return None, None


def _extract_features(bsp) -> dict[str, Any]:
    feats: dict[str, Any] = {}
    for src_key, std_key in FEATURE_KEY_MAP.items():
        try:
            val = bsp.features[src_key]
            # 不覆盖已获取到的非 None 值 (多个源 key 映射到同一目标 key)
            if val is not None or std_key not in feats:
                feats[std_key] = val
        except (KeyError, TypeError, AttributeError):
            if std_key not in feats:
                feats[std_key] = None
    return feats


def _find_parent_event_id(bsp, chan, lv_idx: int, contract: str) -> str | None:
    if lv_idx is None or lv_idx <= 0:
        return None
    try:
        parent_lv = chan.lv_list[lv_idx - 1]
    except (AttributeError, IndexError):
        return None
    try:
        parent_kl = chan[parent_lv]
    except Exception:
        return None
    parent_bsps = getattr(parent_kl, "bs_point_lst", None)
    if parent_bsps is None:
        return None
    klu = getattr(bsp, "klu", None)
    if klu is None:
        return None
    for parent_bsp in parent_bsps.bsp_iter():
        p_klu = getattr(parent_bsp, "klu", None)
        if p_klu is None:
            continue
        for sub_klu in getattr(p_klu, "sub_kl_list", []):
            if hasattr(sub_klu, "idx") and hasattr(klu, "idx") and sub_klu.idx == klu.idx:
                p_primary = _bsp_primary_type(parent_bsp.type)
                p_dir = SignalDirection.LONG if parent_bsp.is_buy else SignalDirection.SHORT
                return new_signal_key(
                    contract,
                    parent_lv.name if hasattr(parent_lv, "name") else str(parent_lv),
                    parent_bsp.bi.idx, p_dir, p_primary)
    return None
