from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
)
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont

from ..common.clickable_line_edit import ClickableLineEdit
from ..code_editor_dialog import CodeEditorDialog


class CodeSourceWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._code_text = ""

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        bold_font = QFont()
        bold_font.setBold(True)

        lbl_code = QLabel("Python 表达式")
        lbl_code.setFont(bold_font)
        header_row.addWidget(lbl_code)
        header_row.addStretch(1)

        layout.addLayout(header_row)

        self._code_preview = ClickableLineEdit()
        self._code_preview.setReadOnly(True)
        self._code_preview.setProperty("codePreview", True)
        self._code_preview.setPlaceholderText("点击打开代码编辑器…")
        self._code_preview.clicked.connect(self._open_code_editor_dialog)
        layout.addWidget(self._code_preview)

        self._sync_code_preview()

    def code_text(self) -> str:
        return self._code_text

    def set_code_text(self, text: str) -> None:
        self._code_text = text
        self._sync_code_preview()

    def summary(self) -> str:
        first_line = self._first_non_empty_line(self._code_text)
        if first_line:
            return (first_line[:28] + "…") if len(first_line) > 28 else first_line
        return ""

    def get_params(self) -> dict:
        return {"code": self._code_text}

    def _open_code_editor_dialog(self):
        dlg = CodeEditorDialog(self, title="代码编辑器")
        dlg.set_mode("generate_data")
        if self._code_text.strip():
            dlg.set_text(self._code_text)
        else:
            dlg.reset_template()
        if dlg.exec():
            self._code_text = dlg.get_text()
            self._sync_code_preview()
            self.changed.emit()

    def _sync_code_preview(self):
        first_line = self._first_non_empty_line(self._code_text)
        if first_line:
            shown = (first_line[:120] + "…") if len(first_line) > 120 else first_line
            self._code_preview.setText(shown)
            self._code_preview.setToolTip(first_line)
        else:
            self._code_preview.clear()
            self._code_preview.setToolTip("")

    @staticmethod
    def _first_non_empty_line(text: str) -> str:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s:
                return s
        return ""
