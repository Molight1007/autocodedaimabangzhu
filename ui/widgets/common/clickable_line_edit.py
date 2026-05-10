from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLineEdit


class ClickableLineEdit(QLineEdit):
    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

