from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .variable_row import VariableRowWidget


class VariablesPanel(QGroupBox):
    configs_changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Variables", parent)
        self.vars_count_label: QLabel
        self.btn_add: QPushButton
        self.btn_load: QPushButton
        self.btn_clear: QPushButton
        self.scroll_area: QScrollArea
        self.vars_container: QWidget
        self.vars_layout: QVBoxLayout
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 26, 16, 16)
        layout.setSpacing(12)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(10)

        self.vars_count_label = QLabel("")
        self.vars_count_label.setStyleSheet("color: #6b6b6f; font-weight: 500;")
        toolbar_layout.addWidget(self.vars_count_label)
        toolbar_layout.addStretch()

        self.btn_add = QPushButton("+ 添加变量")
        self.btn_add.setMinimumHeight(32)
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.setStyleSheet(
            """
            QPushButton {
                background-color: #F2F2F7;
                color: #333333;
                border: 1px solid #d1d1d6;
                border-radius: 6px;
                font-weight: 700;
                padding: 0 14px;
            }
            QPushButton:hover { background-color: #e5e5ea; border-color: #c7c7cc; }
            QPushButton:pressed { background-color: #d1d1d6; }
        """
        )
        self.btn_add.clicked.connect(self.add_variable_row)
        toolbar_layout.addWidget(self.btn_add)

        self.btn_load = QPushButton("加载模板")
        self.btn_load.setMinimumHeight(32)
        self.btn_load.setCursor(Qt.PointingHandCursor)
        self.btn_load.setStyleSheet(
            """
            QPushButton {
                background-color: #ffffff;
                color: #333333;
                border: 1px solid #d1d1d6;
                border-radius: 6px;
                font-weight: 700;
                padding: 0 14px;
            }
            QPushButton:hover { border-color: #007AFF; color: #007AFF; }
        """
        )
        toolbar_layout.addWidget(self.btn_load)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setMinimumHeight(32)
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setStyleSheet(
            """
            QPushButton {
                background-color: #ffffff;
                color: #FF3B30;
                border: 1px solid #e5e5e5;
                border-radius: 6px;
                font-weight: 700;
                padding: 0 12px;
            }
            QPushButton:hover { background-color: #FFF0F0; border-color: #FF3B30; }
        """
        )
        self.btn_clear.clicked.connect(self.clear_all_rows)
        toolbar_layout.addWidget(self.btn_clear)

        layout.addWidget(toolbar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)

        self.vars_container = QWidget()
        self.vars_container.setObjectName("VarsContainer")
        self.vars_container.setStyleSheet("background-color: #f9f9f9;")

        self.vars_layout = QVBoxLayout(self.vars_container)
        self.vars_layout.setSpacing(10)
        self.vars_layout.setContentsMargins(12, 12, 12, 12)
        self.vars_layout.setAlignment(Qt.AlignTop)
        self.vars_layout.addStretch(1)

        self.scroll_area.setWidget(self.vars_container)
        layout.addWidget(self.scroll_area, 1)

        self.add_variable_row()
        self._update_vars_count()

    def get_configs(self) -> List[Dict[str, Any]]:
        configs: List[Dict[str, Any]] = []
        for i in range(self.vars_layout.count() - 1):
            widget = self.vars_layout.itemAt(i).widget()
            if isinstance(widget, VariableRowWidget):
                configs.append(widget.get_config())
        return configs

    def _update_vars_count(self) -> None:
        count = 0
        for i in range(self.vars_layout.count()):
            widget = self.vars_layout.itemAt(i).widget()
            if isinstance(widget, VariableRowWidget):
                count += 1
        text = f"{count} 个变量" if count != 1 else "1 个变量"
        self.vars_count_label.setText(text)

    def add_variable_row(self) -> None:
        row = VariableRowWidget()
        row.delete_requested.connect(self.remove_variable_row)
        self.vars_layout.insertWidget(self.vars_layout.count() - 1, row)
        self._update_vars_count()
        self.configs_changed.emit()

        QApplication.processEvents()
        self.scroll_area.verticalScrollBar().setValue(self.scroll_area.verticalScrollBar().maximum())

    def remove_variable_row(self, row_widget) -> None:
        row_widget.deleteLater()
        self.vars_layout.removeWidget(row_widget)
        self._ensure_has_one_variable()
        self._update_vars_count()
        self.configs_changed.emit()

    def clear_all_rows(self) -> None:
        while self.vars_layout.count() > 1:
            item = self.vars_layout.itemAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                self.vars_layout.removeWidget(widget)
        self.add_variable_row()

    def _ensure_has_one_variable(self) -> None:
        for i in range(self.vars_layout.count()):
            widget = self.vars_layout.itemAt(i).widget()
            if isinstance(widget, VariableRowWidget):
                return
        self.add_variable_row()

