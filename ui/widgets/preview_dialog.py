from __future__ import annotations

import json
from typing import Any, List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.models import VariableConfig
from services.preview.preview_policy import PreviewPolicy, choose_preview_rows, truncate_cell
from services.preview.preview_sample import build_preview_samples
from ..styles.preview_dialog_style import PREVIEW_DIALOG_QSS


class PreviewTableModel(QAbstractTableModel):
    def __init__(self, columns: List[str], rows: List[List[Any]], parent=None, *, policy: Optional[PreviewPolicy] = None):
        super().__init__(parent)
        self._columns = columns
        self._rows = rows
        self._policy = policy or PreviewPolicy()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r = index.row()
        c = index.column()
        if r < 0 or r >= len(self._rows) or c < 0 or c >= len(self._columns):
            return None

        v = self._rows[r][c]
        if role == Qt.DisplayRole:
            if isinstance(v, str):
                return truncate_cell(v, self._policy)
            return v
        if role == Qt.TextAlignmentRole:
            if isinstance(v, (int, float)):
                return int(Qt.AlignVCenter | Qt.AlignRight)
            return int(Qt.AlignVCenter | Qt.AlignLeft)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self._columns):
                return self._columns[section]
            return ""
        return str(section + 1)


class PreviewDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        configs: List[VariableConfig],
        total_rows: int,
        no_duplicates: bool = False,
        policy: Optional[PreviewPolicy] = None,
        title: str = "预览",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(980, 640)

        self._policy = policy or PreviewPolicy()
        self._configs = list(configs or [])
        self._total_rows = max(1, int(total_rows or 1))
        self._row_full_dialog: Optional[QDialog] = None
        self.setStyleSheet(PREVIEW_DIALOG_QSS)

        self._preview_rows = choose_preview_rows(
            total_rows=self._total_rows,
            field_count=len(self._configs),
            policy=self._policy,
        )

        full_policy = PreviewPolicy(
            max_cells=self._policy.max_cells,
            hard_max_rows=self._policy.hard_max_rows,
            hard_min_rows=self._policy.hard_min_rows,
            max_cell_chars=2_000_000,
            max_loop_items_per_cell=self._policy.max_loop_items_per_cell,
        )
        samples = build_preview_samples(
            self._configs,
            self._preview_rows,
            policy=full_policy,
            no_duplicates=bool(no_duplicates),
        )
        self._samples = list(samples or [])
        self._preview_rows = len(samples)
        columns = list(samples[0].keys()) if samples else []
        rows_matrix = [[row.get(col, "") for col in columns] for row in samples]
        self._columns = list(columns or [])
        self._model = PreviewTableModel(columns, rows_matrix, self, policy=self._policy)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)

        title_label = QLabel("预览")
        title_label.setObjectName("Title")
        header_layout.addWidget(title_label)

        hint_label = QLabel("仅展示有限样本，性能优先。")
        hint_label.setObjectName("Hint")
        header_layout.addWidget(hint_label)

        layout.addWidget(header)

        summary_card = QWidget()
        summary_card.setObjectName("SummaryCard")
        summary_layout = QHBoxLayout(summary_card)
        summary_layout.setContentsMargins(12, 10, 12, 10)
        summary_layout.setSpacing(18)

        self._add_summary_kv(summary_layout, "变量数", str(len(self._configs)))
        self._add_summary_kv(summary_layout, "目标行数", str(self._total_rows))
        self._add_summary_kv(summary_layout, "预览行数", str(self._preview_rows))
        self._add_summary_kv(summary_layout, "单元格截断", f"{self._policy.max_cell_chars} 字符")

        summary_layout.addStretch(1)
        layout.addWidget(summary_card)

        self.table = QTableView()
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setModel(self._model)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectItems)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.clicked.connect(self._on_table_clicked)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        r = index.row()
        if r < 0 or r >= len(getattr(self, "_samples", []) or []):
            return
        row = self._samples[r]
        if not isinstance(row, dict) or not row:
            return
        self._open_row_full_preview(row_number=r + 1, row=row)

    def _open_row_full_preview(self, *, row_number: int, row: dict) -> None:
        dlg = self._row_full_dialog
        if dlg is None:
            dlg = _RowFullPreviewDialog(self)
            dlg.setStyleSheet(PREVIEW_DIALOG_QSS)
            dlg.finished.connect(self._on_row_full_dialog_finished)
            self._row_full_dialog = dlg
        if isinstance(dlg, _RowFullPreviewDialog):
            dlg.set_row(row=row, columns=self._columns, row_number=row_number)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_row_full_dialog_finished(self, _result: int) -> None:
        self._row_full_dialog = None

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


class _RowFullPreviewDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("数据全览")
        self.setModal(False)
        self.resize(860, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)

        title_label = QLabel("数据全览")
        title_label.setObjectName("Title")
        header_layout.addWidget(title_label)

        self._hint_label = QLabel("")
        self._hint_label.setObjectName("Hint")
        header_layout.addWidget(self._hint_label)

        layout.addWidget(header)

        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def set_row(self, *, row: dict, columns: List[str], row_number: int) -> None:
        self.setWindowTitle(f"数据全览 - 第 {row_number} 行")
        self._hint_label.setText(f"第 {row_number} 行 · {len(columns or [])} 列")
        self._edit.setPlainText(self._render_row_text(row=row, columns=columns))

    def _render_row_text(self, *, row: dict, columns: List[str]) -> str:
        parts: List[str] = []
        for col in (columns or []):
            v = row.get(col, "")
            parts.append(f"{col}:\n{self._format_value(v)}")
        if not parts:
            return ""
        return ("\n\n" + ("-" * 56) + "\n\n").join(parts).rstrip() + "\n"

    def _format_value(self, v: Any) -> str:
        if isinstance(v, str):
            return (v or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
        try:
            return json.dumps(v, ensure_ascii=False, indent=2)
        except Exception:
            return "" if v is None else str(v)
