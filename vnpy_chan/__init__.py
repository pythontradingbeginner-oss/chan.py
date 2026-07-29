from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from .converter import bar_to_klu, bars_to_ohlc_frame, window_to_kl_type

if TYPE_CHECKING:
    from .app import ChanAnalysisApp
    from .engine import ChanAnalysisEngine, ChanRunConfig
    from .live_engine import LiveTradingEngine
    from .snapshot import ChanSnapshotManager

__all__ = [
    "ChanAnalysisApp",
    "ChanAnalysisEngine",
    "ChanRunConfig",
    "LiveTradingEngine",
    "ChanSnapshotManager",
    "bar_to_klu",
    "bars_to_ohlc_frame",
    "window_to_kl_type",
]


def __getattr__(name: str):
    if name == "ChanAnalysisApp":
        from .app import ChanAnalysisApp

        return ChanAnalysisApp
    if name in {"ChanAnalysisEngine", "ChanRunConfig"}:
        from .engine import ChanAnalysisEngine, ChanRunConfig

        return {"ChanAnalysisEngine": ChanAnalysisEngine, "ChanRunConfig": ChanRunConfig}[name]
    raise AttributeError(name)
