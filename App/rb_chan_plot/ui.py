from __future__ import annotations

from pathlib import Path

from chan_futures.rb_chan_plot import (
    DEFAULT_DATA_PATH,
    DEFAULT_DISPLAY_BARS,
    DEFAULT_END,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_START,
    default_output_filename,
    ensure_png_filename,
    load_datetime_range,
    render_rb_chan_plot,
)


class RbChanPlotWindow:
    def __init__(
        self,
        *,
        data_path: str | Path = DEFAULT_DATA_PATH,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    ) -> None:
        from PySide6 import QtCore, QtGui, QtWidgets

        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._preview_pixmap: QtGui.QPixmap | None = None
        self._filename_user_edited = False
        self._syncing_filename = False

        self.widget = QtWidgets.QWidget()
        self.widget.setWindowTitle("RB 15m 缠论画图保存")
        self.widget.resize(1100, 760)

        self.data_path_edit = QtWidgets.QLineEdit(str(Path(data_path)))
        self.data_path_button = QtWidgets.QPushButton("浏览")
        self.start_edit = QtWidgets.QDateTimeEdit()
        self.end_edit = QtWidgets.QDateTimeEdit()
        for edit in (self.start_edit, self.end_edit):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")

        self.output_dir_edit = QtWidgets.QLineEdit(str(Path(output_dir)))
        self.output_dir_button = QtWidgets.QPushButton("浏览")
        self.filename_edit = QtWidgets.QLineEdit()
        self.display_bars_spin = QtWidgets.QSpinBox()
        self.display_bars_spin.setRange(0, 1_000_000)
        self.display_bars_spin.setSpecialValueText("全部")
        self.display_bars_spin.setValue(DEFAULT_DISPLAY_BARS)
        self.generate_button = QtWidgets.QPushButton("生成")
        self.status_label = QtWidgets.QLabel("就绪")
        self.status_label.setWordWrap(True)

        self.preview_label = QtWidgets.QLabel("图片预览")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(640, 420)
        self.preview_scroll = QtWidgets.QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setWidget(self.preview_label)

        form = QtWidgets.QFormLayout()
        form.addRow("数据文件", self._path_row(self.data_path_edit, self.data_path_button))
        form.addRow("开始时间", self.start_edit)
        form.addRow("结束时间", self.end_edit)
        form.addRow("保存目录", self._path_row(self.output_dir_edit, self.output_dir_button))
        form.addRow("文件名", self.filename_edit)
        form.addRow("显示最近 N 根", self.display_bars_spin)
        form.addRow("", self.generate_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.preview_scroll, stretch=1)
        layout.addWidget(self.status_label)
        self.widget.setLayout(layout)

        self.data_path_button.clicked.connect(self.choose_data_path)
        self.output_dir_button.clicked.connect(self.choose_output_dir)
        self.generate_button.clicked.connect(self.generate_plot)
        self.filename_edit.textEdited.connect(self._mark_filename_edited)
        self.start_edit.dateTimeChanged.connect(self._refresh_default_filename)
        self.end_edit.dateTimeChanged.connect(self._refresh_default_filename)
        self.data_path_edit.editingFinished.connect(self.load_available_range)

        self._set_datetime_defaults(DEFAULT_START, DEFAULT_END)
        self._refresh_default_filename()
        self.load_available_range()

    def show(self) -> None:
        self.widget.show()

    def close(self) -> None:
        self.widget.close()

    def choose_data_path(self) -> None:
        path, _ = self.QtWidgets.QFileDialog.getOpenFileName(
            self.widget,
            "选择 RB 15m parquet",
            str(Path(self.data_path_edit.text()).parent),
            "Parquet 文件 (*.parquet);;所有文件 (*)",
        )
        if path:
            self.data_path_edit.setText(path)
            self.load_available_range()

    def choose_output_dir(self) -> None:
        directory = self.QtWidgets.QFileDialog.getExistingDirectory(
            self.widget,
            "选择保存目录",
            self.output_dir_edit.text(),
        )
        if directory:
            self.output_dir_edit.setText(directory)

    def load_available_range(self) -> None:
        try:
            start, end = load_datetime_range(Path(self.data_path_edit.text()))
        except Exception as exc:  # noqa: BLE001 - show the exact backend message in the UI
            self.status_label.setText(f"读取数据范围失败：{exc}")
            return
        self._set_datetime_defaults(start, end)
        self._refresh_default_filename()
        self.status_label.setText(f"数据范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}")

    def generate_plot(self) -> None:
        start = self.start_edit.dateTime().toPython()
        end = self.end_edit.dateTime().toPython()
        if start > end:
            self.QtWidgets.QMessageBox.warning(self.widget, "时间范围不正确", "开始时间必须早于或等于结束时间。")
            return

        filename = self.filename_edit.text().strip() or default_output_filename(start, end)
        filename = ensure_png_filename(filename)
        output_dir = Path(self.output_dir_edit.text())
        output_path = output_dir / filename
        overwrite = False
        if output_path.exists():
            answer = self.QtWidgets.QMessageBox.question(
                self.widget,
                "覆盖确认",
                f"文件已存在，是否覆盖？\n{output_path}",
            )
            if answer != self.QtWidgets.QMessageBox.StandardButton.Yes:
                return
            overwrite = True

        self.generate_button.setEnabled(False)
        self.status_label.setText("正在生成图片...")
        self.QtWidgets.QApplication.setOverrideCursor(self.QtCore.Qt.CursorShape.WaitCursor)
        try:
            result = render_rb_chan_plot(
                data_path=Path(self.data_path_edit.text()),
                output_dir=output_dir,
                filename=filename,
                start=start,
                end=end,
                display_bars=int(self.display_bars_spin.value()),
                overwrite=overwrite,
            )
            self._load_preview(result.output_path)
            self.status_label.setText(
                "已保存：{path}\nK 线：{bars} 根，绘图：{plotted} 根，"
                "时间：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}，"
                "买卖点：{bsp}，笔：{bi}，线段：{seg}，中枢：{zs}".format(
                    path=result.output_path,
                    bars=result.bar_count,
                    plotted=result.plotted_bar_count,
                    start=result.start_time,
                    end=result.end_time,
                    bsp=result.bsp_count,
                    bi=result.bi_count,
                    seg=result.seg_count,
                    zs=result.zs_count,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surface backend validation errors
            self.status_label.setText(f"生成失败：{exc}")
            self.QtWidgets.QMessageBox.critical(self.widget, "生成失败", str(exc))
        finally:
            self.QtWidgets.QApplication.restoreOverrideCursor()
            self.generate_button.setEnabled(True)

    def _path_row(self, edit, button):
        row = self.QtWidgets.QWidget()
        layout = self.QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, stretch=1)
        layout.addWidget(button)
        return row

    def _set_datetime_defaults(self, start, end) -> None:
        self.start_edit.setDateTime(self.QtCore.QDateTime(start))
        self.end_edit.setDateTime(self.QtCore.QDateTime(end))

    def _mark_filename_edited(self) -> None:
        if not self._syncing_filename:
            self._filename_user_edited = True

    def _refresh_default_filename(self) -> None:
        if self._filename_user_edited:
            return
        self._syncing_filename = True
        try:
            start = self.start_edit.dateTime().toPython()
            end = self.end_edit.dateTime().toPython()
            self.filename_edit.setText(default_output_filename(start, end))
        finally:
            self._syncing_filename = False

    def _load_preview(self, path: Path) -> None:
        pixmap = self.QtGui.QPixmap(str(path))
        if pixmap.isNull():
            self.preview_label.setText("图片预览加载失败")
            self._preview_pixmap = None
            return
        self._preview_pixmap = pixmap
        viewport_size = self.preview_scroll.viewport().size()
        scaled = pixmap.scaled(
            viewport_size,
            self.QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            self.QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)


def main() -> None:
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = RbChanPlotWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
