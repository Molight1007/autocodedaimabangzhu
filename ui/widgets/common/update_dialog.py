from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...styles.update_dialog_style import UPDATE_DIALOG_QSS


class UpdateDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        current_version: str,
        latest_version: str,
        notes: str,
        download_url: str,
        published_at: Optional[str] = None,
        title: str = "发现新版本",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(720, 480)
        self.setStyleSheet(UPDATE_DIALOG_QSS)

        self.download_url = download_url

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)

        title_label = QLabel(title)
        title_label.setObjectName("DialogTitle")
        header_layout.addWidget(title_label)

        hint_label = QLabel("建议更新到最新版本，以获得更稳定的体验。")
        hint_label.setObjectName("DialogHint")
        header_layout.addWidget(hint_label)

        layout.addWidget(header)

        summary_card = QWidget()
        summary_card.setObjectName("SummaryCard")
        summary_layout = QHBoxLayout(summary_card)
        summary_layout.setContentsMargins(12, 10, 12, 10)
        summary_layout.setSpacing(18)

        self._add_summary_kv(summary_layout, "当前版本", current_version)
        self._add_summary_kv(summary_layout, "最新版本", latest_version)
        if published_at:
            self._add_summary_kv(summary_layout, "发布时间", published_at)

        summary_layout.addStretch(1)
        layout.addWidget(summary_card)

        notes_title = QLabel("更新说明")
        notes_title.setObjectName("NotesTitle")
        layout.addWidget(notes_title)

        self.notes_box = QPlainTextEdit()
        self.notes_box.setObjectName("NotesBox")
        self.notes_box.setReadOnly(True)
        self.notes_box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.notes_box.setPlainText(notes or "暂无更新说明。")
        self.notes_box.setMinimumHeight(180)
        layout.addWidget(self.notes_box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_ok = buttons.button(QDialogButtonBox.Ok)
        btn_ok.setText("立即下载")
        btn_ok.setObjectName("PrimaryBtn")
        btn_cancel = buttons.button(QDialogButtonBox.Cancel)
        btn_cancel.setText("稍后提醒")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_summary_kv(self, layout: QHBoxLayout, key: str, value: str) -> None:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        k = QLabel(key)
        k.setObjectName("SummaryKey")
        col.addWidget(k)

        v = QLabel(value)
        v.setObjectName("SummaryValue")
        col.addWidget(v)

        layout.addWidget(box)

    @staticmethod
    def demo(parent=None) -> "UpdateDialog":
        return UpdateDialog(
            parent,
            current_version="0.1.0",
            latest_version="0.2.0",
            notes="1. 新增批量导出功能\n2. 更新模板管理入口\n3. 修复已知崩溃问题",
            download_url="https://example.com/download",
            published_at="2026-03-09",
        )
