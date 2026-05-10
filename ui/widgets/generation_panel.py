from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QVBoxLayout,
)

from services.preview.output_cleanup import count_deletable_files, delete_output_dir_contents, is_safe_directory_target


class GenerationPanel(QGroupBox):
    preview_requested = Signal()
    generate_requested = Signal()
    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__("Generation Settings", parent)
        self.count_input: QLineEdit
        self.chk_no_duplicates: QCheckBox
        self.chk_delete_temp: QCheckBox
        self.total_score_input: QLineEdit
        self.chk_use_score: QCheckBox
        self.output_dir_input: QLineEdit
        self.btn_browse: QPushButton
        self.btn_delete_old: QPushButton
        self.combo_delete_time: QComboBox
        self.btn_delete_month: QPushButton
        self.btn_delete_year: QPushButton
        self.btn_preview: QPushButton
        self.btn_generate: QPushButton
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 26, 16, 16)
        layout.setSpacing(12)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        lbl_count = QLabel("数量")
        lbl_count.setStyleSheet("font-weight: 700;")
        row1.addWidget(lbl_count)

        self.count_input = QLineEdit("10")
        self.count_input.setFixedWidth(90)
        self.count_input.setAlignment(Qt.AlignCenter)
        row1.addWidget(self.count_input)

        row1.addSpacing(12)
        self.chk_no_duplicates = QCheckBox("不生成重复")
        row1.addWidget(self.chk_no_duplicates)

        self.chk_delete_temp = QCheckBox("生成ZIP后删除临时文件")
        self.chk_delete_temp.setChecked(True)
        row1.addWidget(self.chk_delete_temp)

        row1.addStretch()
        layout.addLayout(row1)

        score_row = QHBoxLayout()
        score_row.setSpacing(10)

        lbl_total_score = QLabel("总得分")
        lbl_total_score.setStyleSheet("font-weight: 700;")
        score_row.addWidget(lbl_total_score)

        self.total_score_input = QLineEdit("100")
        self.total_score_input.setFixedWidth(90)
        self.total_score_input.setAlignment(Qt.AlignCenter)
        self.total_score_input.setValidator(QIntValidator(1, 100000))
        score_row.addWidget(self.total_score_input)

        score_row.addSpacing(12)

        self.chk_use_score = QCheckBox("使用评分")
        self.chk_use_score.setChecked(False)
        score_row.addWidget(self.chk_use_score)

        score_row.addStretch()
        layout.addLayout(score_row)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(10)
        lbl_dir = QLabel("输出目录")
        lbl_dir.setStyleSheet("font-weight: 700;")
        dir_row.addWidget(lbl_dir)

        self.output_dir_input = QLineEdit()
        self.output_dir_input.setPlaceholderText("选择或输入导出路径…")
        dir_row.addWidget(self.output_dir_input, 1)

        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self.browse_output_dir)
        dir_row.addWidget(self.btn_browse)

        layout.addLayout(dir_row)

        cleanup_row = QHBoxLayout()
        cleanup_row.setSpacing(10)
        cleanup_row.addWidget(QLabel("清理旧数据"))

        self.btn_delete_old = QPushButton("删除")
        self.btn_delete_old.setCursor(Qt.PointingHandCursor)
        self.btn_delete_old.clicked.connect(self._on_delete_old_clicked)
        cleanup_row.addWidget(self.btn_delete_old)

        self.combo_delete_time = QComboBox()
        self.combo_delete_time.addItems(["一天内", "一周内", "所有"])
        cleanup_row.addWidget(self.combo_delete_time)

        self.btn_delete_month = QPushButton("一月内")
        self.btn_delete_month.setCursor(Qt.PointingHandCursor)
        self.btn_delete_month.clicked.connect(lambda: self._on_delete_old_clicked(days=30))
        cleanup_row.addWidget(self.btn_delete_month)

        self.btn_delete_year = QPushButton("一年内")
        self.btn_delete_year.setCursor(Qt.PointingHandCursor)
        self.btn_delete_year.clicked.connect(lambda: self._on_delete_old_clicked(days=365))
        cleanup_row.addWidget(self.btn_delete_year)

        cleanup_row.addStretch()
        layout.addLayout(cleanup_row)

        layout.addStretch(1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 6, 0, 0)
        actions.setSpacing(12)
        actions.addStretch()

        self.btn_preview = QPushButton("预览")
        self.btn_preview.setFixedHeight(42)
        self.btn_preview.setMinimumWidth(96)
        self.btn_preview.setCursor(Qt.PointingHandCursor)
        self.btn_preview.setStyleSheet(
            """
            QPushButton {
                background-color: #ffffff;
                color: #333333;
                font-weight: 700;
                font-size: 14px;
                border: 1px solid #d1d1d6;
                border-radius: 8px;
                padding: 0 18px;
            }
            QPushButton:hover { border-color: #007AFF; color: #007AFF; }
        """
        )
        self.btn_preview.clicked.connect(self.preview_requested.emit)
        actions.addWidget(self.btn_preview)

        self.btn_generate = QPushButton("生成测试数据")
        self.btn_generate.setFixedHeight(42)
        self.btn_generate.setMinimumWidth(160)
        self.btn_generate.setCursor(Qt.PointingHandCursor)
        self.btn_generate.setDefault(True)
        self.btn_generate.setStyleSheet(
            """
            QPushButton {
                background-color: #007AFF;
                color: white;
                font-weight: 800;
                font-size: 14px;
                border-radius: 8px;
                border: none;
                padding: 0 22px;
            }
            QPushButton:hover { background-color: #0062cc; }
            QPushButton:pressed { background-color: #004999; }
        """
        )
        self.btn_generate.clicked.connect(self.generate_requested.emit)
        actions.addWidget(self.btn_generate)

        layout.addLayout(actions)
        self.setMinimumWidth(380)

    def browse_output_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if dir_path:
            self.output_dir_input.setText(dir_path)

    def get_total_rows(self) -> int:
        try:
            return max(1, int(self.count_input.text()))
        except Exception:
            return 1

    def get_no_duplicates(self) -> bool:
        return bool(self.chk_no_duplicates.isChecked())

    def get_output_dir(self) -> str:
        return str(self.output_dir_input.text() or "").strip()

    def get_use_score(self) -> bool:
        return bool(self.chk_use_score.isChecked())

    def get_total_score(self) -> int:
        try:
            return max(1, int(self.total_score_input.text()))
        except Exception:
            return 100

    def load_from_qsettings(self, settings: QSettings) -> None:
        val_count = settings.value("gen/count", "10")
        self.count_input.setText(str(val_count))

        self.chk_no_duplicates.setChecked(settings.value("gen/no_duplicates", False, type=bool))
        self.chk_delete_temp.setChecked(settings.value("gen/delete_temp", True, type=bool))

        val_dir = settings.value("gen/output_dir", "")
        self.output_dir_input.setText(str(val_dir))

        idx = settings.value("gen/delete_old_idx", 0, type=int)
        if 0 <= idx < self.combo_delete_time.count():
            self.combo_delete_time.setCurrentIndex(idx)

        val_score = settings.value("gen/total_score", "100")
        self.total_score_input.setText(str(val_score))

        self.chk_use_score.setChecked(settings.value("gen/use_score", False, type=bool))

    def save_to_qsettings(self, settings: QSettings) -> None:
        settings.setValue("gen/count", self.count_input.text())
        settings.setValue("gen/no_duplicates", self.chk_no_duplicates.isChecked())
        settings.setValue("gen/delete_temp", self.chk_delete_temp.isChecked())
        settings.setValue("gen/output_dir", self.output_dir_input.text())
        settings.setValue("gen/delete_old_idx", self.combo_delete_time.currentIndex())
        settings.setValue("gen/total_score", self.total_score_input.text())
        settings.setValue("gen/use_score", self.chk_use_score.isChecked())

    def _on_delete_old_clicked(self, checked: bool = False, *, days: Optional[int] = None):
        _ = checked
        out_dir = self.get_output_dir()
        if not out_dir:
            QMessageBox.information(self, "提示", "请先设置输出目录。")
            return
        if not is_safe_directory_target(out_dir):
            QMessageBox.information(self, "提示", "输出目录无效，或不允许删除该路径。")
            return

        if days is None:
            idx = int(self.combo_delete_time.currentIndex())
            if idx == 0:
                days = 1
            elif idx == 1:
                days = 7
            else:
                days = None

        if days is None:
            cutoff_ts = None
            scope_text = "所有"
        else:
            cutoff_ts = time.time() - float(days) * 86400.0
            scope_text = f"{days} 天前"

        try:
            cand_files, cand_dirs = count_deletable_files(out_dir, cutoff_ts=cutoff_ts)
        except Exception as e:
            QMessageBox.information(self, "提示", f"扫描失败：{e}")
            return

        if cand_files <= 0 and cand_dirs <= 0:
            QMessageBox.information(self, "提示", "没有可删除的旧数据。")
            return

        if cutoff_ts is None:
            preview_text = f"将删除输出目录下 {cand_files} 个文件、{cand_dirs} 个文件夹。"
        else:
            preview_text = f"将删除输出目录下修改时间早于 {scope_text} 的 {cand_files} 个文件。"

        r = QMessageBox.question(
            self,
            "确认删除",
            preview_text + "\n\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return

        try:
            deleted_files, deleted_dirs = delete_output_dir_contents(out_dir, cutoff_ts=cutoff_ts)
        except Exception as e:
            QMessageBox.information(self, "提示", f"删除失败：{e}")
            return

        if cutoff_ts is None:
            msg = f"已删除：{deleted_files} 个文件、{deleted_dirs} 个文件夹。"
        else:
            msg = f"已删除：{deleted_files} 个文件。"

        self.status_message.emit(msg)
        QMessageBox.information(self, "提示", msg)

