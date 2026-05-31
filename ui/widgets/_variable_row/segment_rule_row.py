from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont


class SegmentRuleRow(QWidget):
    changed = Signal()
    remove_requested = Signal(QWidget)

    def __init__(
        self,
        distribution_registry,
        segment_mode_map: Dict[str, str],
        parent=None,
    ):
        super().__init__(parent)
        self._distribution_registry = distribution_registry
        self._segment_mode_map = segment_mode_map
        self._sub_params_widget: QWidget | None = None
        self._sub_param_inputs: Dict[str, QLineEdit] = {}
        self._segment_distribution_map: Dict[str, str] = {}

        self._bold_font = QFont()
        self._bold_font.setBold(True)

        self._setup_ui()

    def _setup_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(2)

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        self._prob_input = QLineEdit("30")
        self._prob_input.setFixedWidth(50)
        self._prob_input.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(self._prob_input)

        prob_suffix = QLabel("%")
        row_layout.addWidget(prob_suffix)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(list(self._segment_mode_map.keys()))
        row_layout.addWidget(self._mode_combo)

        self._value_input_a = QLineEdit()
        self._value_input_a.setFixedWidth(72)
        row_layout.addWidget(self._value_input_a)

        self._between_label = QLabel("~")
        row_layout.addWidget(self._between_label)

        self._value_input_b = QLineEdit()
        self._value_input_b.setFixedWidth(72)
        row_layout.addWidget(self._value_input_b)

        self._dist_combo = QComboBox()
        sub_entries = self._distribution_registry.get_sub_distribution_entries()
        for e in sub_entries:
            label = e.name
            self._segment_distribution_map[label] = e.key
            self._dist_combo.addItem(label)
        row_layout.addWidget(self._dist_combo)

        delete_btn = QPushButton("删除")
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setFixedHeight(28)
        row_layout.addWidget(delete_btn)
        row_layout.addStretch(1)

        outer_layout.addWidget(row_widget)

        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._prob_input.textChanged.connect(lambda: self.changed.emit())
        self._value_input_a.textChanged.connect(lambda: self.changed.emit())
        self._value_input_b.textChanged.connect(lambda: self.changed.emit())
        self._dist_combo.currentIndexChanged.connect(lambda _: self.changed.emit())
        self._dist_combo.currentIndexChanged.connect(self._update_sub_params)
        delete_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        self._on_mode_changed()

    def _on_mode_changed(self):
        mode = self._segment_mode_map.get(self._mode_combo.currentText(), "range")
        if mode == "range":
            self._value_input_a.setVisible(True)
            self._between_label.setVisible(True)
            self._value_input_b.setVisible(True)
            self._value_input_a.setPlaceholderText("最小值")
            self._value_input_b.setPlaceholderText("最大值")
            self._dist_combo.setVisible(True)
        elif mode == "fixed":
            self._value_input_a.setVisible(True)
            self._between_label.setVisible(False)
            self._value_input_b.setVisible(False)
            self._value_input_a.setPlaceholderText("固定值")
            self._dist_combo.setVisible(False)
        else:
            self._value_input_a.setVisible(True)
            self._between_label.setVisible(False)
            self._value_input_b.setVisible(False)
            self._value_input_a.setPlaceholderText("留空使用默认边界")
            self._dist_combo.setVisible(False)

        self._update_sub_params()
        self.changed.emit()

    def _update_sub_params(self):
        mode = self._segment_mode_map.get(self._mode_combo.currentText(), "range")
        sub_dist_label = self._dist_combo.currentText()
        sub_dist_key = self._segment_distribution_map.get(sub_dist_label, "uniform")

        if self._sub_params_widget is not None:
            self._sub_params_widget.hide()
            layout = self._sub_params_widget.parent().layout() if self._sub_params_widget.parent() else None
            if layout:
                layout.removeWidget(self._sub_params_widget)
            self._sub_params_widget.deleteLater()
            self._sub_params_widget = None
            self._sub_param_inputs = {}

        if mode != "range" or sub_dist_key == "uniform":
            return

        entry = self._distribution_registry.get_entry(sub_dist_key)
        if entry is None or not entry.params:
            return

        sub_widget = QWidget()
        sub_layout = QHBoxLayout(sub_widget)
        sub_layout.setContentsMargins(0, 0, 0, 0)
        sub_layout.setSpacing(4)

        inputs: Dict[str, QLineEdit] = {}
        for p_def in entry.params:
            label = QLabel(p_def["label"])
            label.setFont(self._bold_font)
            inp = QLineEdit()
            inp.setFixedWidth(60)
            placeholder = p_def.get("placeholder", "")
            if placeholder:
                inp.setPlaceholderText(placeholder)
            default_val = p_def.get("default")
            if default_val is not None:
                inp.setText(str(default_val))
            inp.textChanged.connect(lambda: self.changed.emit())
            inputs[p_def["key"]] = inp
            sub_layout.addWidget(label)
            sub_layout.addWidget(inp)

        sub_layout.addStretch(1)

        self._sub_params_widget = sub_widget
        self._sub_param_inputs = inputs

        self.layout().addWidget(sub_widget)
        sub_widget.show()

    def get_data(self) -> Dict[str, Any]:
        mode = self._segment_mode_map.get(self._mode_combo.currentText(), "range")
        segment: Dict[str, Any] = {
            "weight": self._prob_input.text().strip() or "0",
            "mode": mode,
        }
        value_a = self._value_input_a.text().strip()
        value_b = self._value_input_b.text().strip()

        if mode == "range":
            segment["min"] = value_a
            segment["max"] = value_b
            sub_dist_label = self._dist_combo.currentText()
            sub_dist_key = self._segment_distribution_map.get(sub_dist_label, "uniform")
            if sub_dist_key != "uniform":
                segment["distribution"] = sub_dist_key
                if self._sub_param_inputs:
                    dist_params = {}
                    for key, inp in self._sub_param_inputs.items():
                        val = inp.text().strip()
                        if val:
                            dist_params[key] = val
                    if dist_params:
                        segment["distribution_params"] = dist_params
        elif mode == "fixed":
            segment["value"] = value_a
        else:
            if value_a:
                segment["edges"] = value_a

        return segment
