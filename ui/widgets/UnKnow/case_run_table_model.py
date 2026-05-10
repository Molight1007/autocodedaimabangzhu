"""
测试用例运行结果表格模型

功能说明：
- 为测试运行结果提供表格数据模型（MVC中的Model层）
- 支持展示：序号、状态、耗时、stdin/stdout/stderr预览
- 支持动态更新单条用例的执行结果

当前使用状态：
- ⚠️ 截至2026-04-16，经全局搜索确认，本类未被任何文件显式导入使用
- 当前测试运行窗口（test_run_dialog.py）使用QPlainTextEdit展示结果，未使用表格视图
- 可能是历史遗留代码，或为将来功能预留

如需使用：
- 与QTableView配合，展示批量测试用例的运行结果
- 适合需要同时查看多条用例状态的场景

作者备注：
- 保留原因：功能完整，可能用于后续扩展
- 如长期未使用，可考虑删除
"""
from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


def _shorten(s: str, max_len: int) -> str:
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(t) <= max_len:
        return t
    return t[: max(0, max_len - 1)] + "…"


class CaseRunTableModel(QAbstractTableModel):
    _headers = ["#", "状态", "耗时(ms)", "stdin(预览)", "stdout(预览)", "stderr(预览)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = []

    def set_cases(self, cases: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = []
        for i, case in enumerate(cases or [], start=1):
            stdin_text = str((case or {}).get("stdin") or "")
            self._rows.append(
                {
                    "index": i,
                    "status": "Pending",
                    "ms": "",
                    "stdin": stdin_text,
                    "stdout": "",
                    "stderr": "",
                    "stdin_preview": _shorten(stdin_text, 80).replace("\n", "\\n"),
                    "stdout_preview": "",
                    "stderr_preview": "",
                }
            )
        self.endResetModel()

    def get_row(self, row_idx: int) -> Dict[str, Any]:
        if row_idx < 0 or row_idx >= len(self._rows):
            return {}
        return dict(self._rows[row_idx])

    def update_result(
        self,
        row_idx: int,
        *,
        status: str,
        elapsed_ms: Any = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        if row_idx < 0 or row_idx >= len(self._rows):
            return
        row = dict(self._rows[row_idx])
        row["status"] = status
        if elapsed_ms is not None:
            row["ms"] = elapsed_ms
        elif status == "Pending":
            row["ms"] = ""
        row["stdout"] = stdout or ""
        row["stderr"] = stderr or ""
        row["stdout_preview"] = _shorten(row["stdout"], 80).replace("\n", "\\n")
        row["stderr_preview"] = _shorten(row["stderr"], 80).replace("\n", "\\n")
        self._rows[row_idx] = row
        tl = self.index(row_idx, 0)
        br = self.index(row_idx, self.columnCount() - 1)
        self.dataChanged.emit(tl, br, [Qt.DisplayRole, Qt.TextAlignmentRole])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self._headers):
                return self._headers[section]
            return ""
        return str(section + 1)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r = index.row()
        c = index.column()
        if r < 0 or r >= len(self._rows):
            return None
        row = self._rows[r]
        if role == Qt.DisplayRole:
            if c == 0:
                return row.get("index", r + 1)
            if c == 1:
                return row.get("status", "")
            if c == 2:
                v = row.get("ms")
                return "" if v is None else str(v)
            if c == 3:
                return row.get("stdin_preview", "")
            if c == 4:
                return row.get("stdout_preview", "")
            if c == 5:
                return row.get("stderr_preview", "")
        if role == Qt.TextAlignmentRole:
            if c in {0, 2}:
                return int(Qt.AlignVCenter | Qt.AlignRight)
            return int(Qt.AlignVCenter | Qt.AlignLeft)
        return None

