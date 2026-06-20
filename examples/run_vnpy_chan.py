from __future__ import annotations

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_chan import ChanAnalysisApp


def main() -> None:
    qapp = create_qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(ChanAnalysisApp)
    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()
    qapp.exec()


if __name__ == "__main__":
    main()
