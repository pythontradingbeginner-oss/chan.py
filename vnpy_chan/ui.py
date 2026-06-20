from __future__ import annotations

from datetime import datetime

from .engine import ChanAnalysisEngine, ChanRunConfig


class ChanAnalysisWidget:
    def __init__(self, main_engine, event_engine) -> None:
        from PySide6 import QtCore, QtWidgets
        from vnpy.trader.constant import Exchange, Interval

        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.engine: ChanAnalysisEngine = main_engine.get_engine("ChanAnalysis")

        self.widget = QtWidgets.QWidget()
        self.widget.setWindowTitle("缠论分析")
        self.symbol_edit = QtWidgets.QLineEdit("RB%")
        self.exchange_combo = QtWidgets.QComboBox()
        self.exchange_combo.addItem(Exchange.SHFE.value)
        self.window_combo = QtWidgets.QComboBox()
        self.window_combo.addItems(["1", "15", "30", "60"])
        self.start_edit = QtWidgets.QLineEdit()
        self.end_edit = QtWidgets.QLineEdit()
        self.fee_spin = QtWidgets.QDoubleSpinBox()
        self.fee_spin.setRange(0, 9999)
        self.fee_spin.setValue(1.0)
        self.slippage_spin = QtWidgets.QDoubleSpinBox()
        self.slippage_spin.setRange(0, 9999)
        self.slippage_spin.setValue(1.0)
        self.long_only_check = QtWidgets.QCheckBox("只做多")
        self.run_button = QtWidgets.QPushButton("运行")
        self.summary = QtWidgets.QTextEdit()
        self.summary.setReadOnly(True)
        self.fills = QtWidgets.QTableWidget()

        form = QtWidgets.QFormLayout()
        form.addRow("合约/模式", self.symbol_edit)
        form.addRow("交易所", self.exchange_combo)
        form.addRow("周期窗口", self.window_combo)
        form.addRow("开始时间", self.start_edit)
        form.addRow("结束时间", self.end_edit)
        form.addRow("手续费点数", self.fee_spin)
        form.addRow("滑点点数", self.slippage_spin)
        form.addRow("", self.long_only_check)
        form.addRow("", self.run_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(self.fills)
        self.widget.setLayout(layout)
        self.run_button.clicked.connect(self.run_analysis)

        self.Exchange = Exchange
        self.Interval = Interval

    def show(self) -> None:
        self.widget.show()

    def close(self) -> None:
        self.widget.close()

    def run_analysis(self) -> None:
        config = ChanRunConfig(
            symbol_pattern=self.symbol_edit.text().strip() or "RB%",
            exchange=self.Exchange(self.exchange_combo.currentText()),
            interval=self.Interval.MINUTE,
            window=int(self.window_combo.currentText()),
            start=_parse_datetime(self.start_edit.text()),
            end=_parse_datetime(self.end_edit.text()),
            fee_points=float(self.fee_spin.value()),
            slippage_points=float(self.slippage_spin.value()),
            long_only=self.long_only_check.isChecked(),
        )
        result = self.engine.run_from_database(config)
        self.summary.setPlainText(str(self.engine.summarize_result(result)))
        self._show_fills(result.fills)

    def _show_fills(self, fills) -> None:
        self.fills.clear()
        self.fills.setRowCount(len(fills))
        self.fills.setColumnCount(len(fills.columns))
        self.fills.setHorizontalHeaderLabels([str(column) for column in fills.columns])
        for row_idx, row in fills.reset_index(drop=True).iterrows():
            for col_idx, value in enumerate(row):
                self.fills.setItem(row_idx, col_idx, self.QtWidgets.QTableWidgetItem(str(value)))


def _parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    return datetime.fromisoformat(value)
