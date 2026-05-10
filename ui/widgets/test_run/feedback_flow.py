from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QVBoxLayout


def collect_feedback_input(parent: QDialog, *, current_stdin: str) -> Optional[Dict[str, str]]:
    dlg = QDialog(parent)
    dlg.setWindowTitle("反馈重试")
    dlg.resize(760, 520)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    layout.addWidget(QLabel("错误输入数据（可选，尽量只贴最小复现）"))
    stdin_edit = QPlainTextEdit()
    if len(str(current_stdin or "")) <= 2000:
        stdin_edit.setPlainText(str(current_stdin or ""))
    else:
        stdin_edit.setPlainText("")
    layout.addWidget(stdin_edit, 2)

    layout.addWidget(QLabel("期望输出或正确行为（建议填写）"))
    expected_edit = QPlainTextEdit()
    layout.addWidget(expected_edit, 1)

    layout.addWidget(QLabel("错误说明（必填）"))
    note_edit = QPlainTextEdit()
    note_edit.setPlaceholderText("例如：第 3 行少输出一个空格；n=1 时应返回 0；当前把边界条件写反了")
    layout.addWidget(note_edit, 1)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() != QDialog.Accepted:
        return None
    note_text = (note_edit.toPlainText() or "").strip()
    if len(note_text) < 4:
        QMessageBox.information(parent, "提示", "请填写错误说明（至少 4 个字）。")
        return None
    return {
        "stdin": stdin_edit.toPlainText(),
        "expected": expected_edit.toPlainText(),
        "note": note_text,
    }


def build_feedback_payload(
    *,
    action: str,
    index: int,
    run: Dict[str, Any],
    extra: Dict[str, str],
    clip_text: Callable[[str, int], str],
) -> Dict[str, Any]:
    return {
        "action": str(action or "feedback_retry"),
        "index": int(index),
        "status": str(run.get("status") or ""),
        "timed_out": bool(run.get("timed_out")),
        "feedback_stdin": clip_text(str(extra.get("stdin") or ""), 4000),
        "feedback_expected": clip_text(str(extra.get("expected") or ""), 4000),
        "feedback_note": clip_text(str(extra.get("note") or ""), 4000),
    }
