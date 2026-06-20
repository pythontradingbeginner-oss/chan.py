from __future__ import annotations

from pathlib import Path

from vnpy.trader.app import BaseApp

from .engine import APP_NAME, ChanAnalysisEngine


class ChanAnalysisApp(BaseApp):
    app_name = APP_NAME
    app_module = "vnpy_chan"
    app_path = Path(__file__).parent
    display_name = "缠论分析"
    engine_class = ChanAnalysisEngine
    widget_name = "ChanAnalysisWidget"
    icon_name = ""
