from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from ...styles.api_key_dialog_style import API_KEY_DIALOG_QSS


class ApiKeyDialog(QDialog):
    def __init__(self, parent=None, *, title: str = "配置 API Key", provider_label: str = "AI"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(720, 320)
        self.setStyleSheet(API_KEY_DIALOG_QSS)
        self._provider_label = str(provider_label or "AI").strip() or "AI"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)

        title_label = QLabel(f"{self._provider_label} API Key")
        title_label.setObjectName("Title")
        header_layout.addWidget(title_label)

        hint_label = QLabel("仅保存在本机当前用户下，应用不会显示或上传你的 Key。")
        hint_label.setObjectName("Hint")
        header_layout.addWidget(hint_label)

        layout.addWidget(header)

        card = QWidget()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 12, 12, 12)
        card_layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)

        field_label = QLabel("API Key")
        field_label.setObjectName("FieldLabel")
        field_label.setFixedWidth(70)
        row.addWidget(field_label)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText(f"粘贴你的 {self._provider_label} API Key（不回显）")
        self.key_input.setEchoMode(QLineEdit.Password)
        row.addWidget(self.key_input, 1)

        self.btn_toggle = QPushButton("显示")
        self.btn_toggle.setCursor(Qt.PointingHandCursor)
        self.btn_toggle.clicked.connect(self._toggle_echo)
        row.addWidget(self.btn_toggle)

        card_layout.addLayout(row)
        layout.addWidget(card)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_ok = buttons.button(QDialogButtonBox.Ok)
        btn_ok.setText("保存")
        btn_ok.setObjectName("PrimaryBtn")

        btn_cancel = buttons.button(QDialogButtonBox.Cancel)
        btn_cancel.setText("取消")

        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.key_input.setFocus()

    def get_api_key(self) -> str:
        return (self.key_input.text() or "").strip()

    def _toggle_echo(self):
        if self.key_input.echoMode() == QLineEdit.Password:
            self.key_input.setEchoMode(QLineEdit.Normal)
            self.btn_toggle.setText("隐藏")
        else:
            self.key_input.setEchoMode(QLineEdit.Password)
            self.btn_toggle.setText("显示")

    def _on_accept(self):
        k = self.get_api_key()
        if len(k) < 8:
            QMessageBox.information(self, "提示", "API Key 不能为空。")
            return
        self.accept()
