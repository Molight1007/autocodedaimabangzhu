from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


def build_test_run_dialog_ui(dialog: QDialog, *, case_count: int) -> Dict[str, Any]:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(12)

    header = QWidget()
    header.setObjectName("DialogHeader")
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(0, 0, 0, 0)
    header_layout.setSpacing(2)

    title = QLabel("测试运行")
    title.setObjectName("DialogTitle")
    header_layout.addWidget(title)

    hint = QLabel(f"示例数据：{int(case_count)} 条。打开后自动执行示例，可手动输入单次运行。")
    hint.setObjectName("DialogHint")
    header_layout.addWidget(hint)
    layout.addWidget(header)

    toolbar = QWidget()
    toolbar.setObjectName("Toolbar")
    toolbar_layout = QHBoxLayout(toolbar)
    toolbar_layout.setContentsMargins(10, 8, 10, 8)
    toolbar_layout.setSpacing(10)

    progress_label = QLabel("0/0")
    progress_label.setObjectName("ModeLabel")
    toolbar_layout.addWidget(progress_label)

    toolbar_layout.addStretch(1)

    btn_prev = QPushButton("上一个")
    btn_prev.setCursor(Qt.PointingHandCursor)
    toolbar_layout.addWidget(btn_prev)

    btn_next = QPushButton("下一个")
    btn_next.setCursor(Qt.PointingHandCursor)
    toolbar_layout.addWidget(btn_next)

    layout.addWidget(toolbar)

    detail_split = QSplitter(Qt.Horizontal)
    detail_split.setChildrenCollapsible(False)

    input_card = QWidget()
    input_layout = QVBoxLayout(input_card)
    input_layout.setContentsMargins(0, 0, 0, 0)
    input_layout.setSpacing(8)

    input_layout.addWidget(QLabel("当前数据（case）"))
    case_edit = QPlainTextEdit()
    case_edit.setReadOnly(True)
    input_layout.addWidget(case_edit, 2)

    input_layout.addWidget(QLabel("输入数据（stdin）"))
    input_toolbar = QHBoxLayout()
    input_toolbar.setSpacing(8)
    btn_prev_input = QPushButton("上一条输入")
    btn_prev_input.setCursor(Qt.PointingHandCursor)
    input_toolbar.addWidget(btn_prev_input)

    btn_next_input = QPushButton("下一条输入")
    btn_next_input.setCursor(Qt.PointingHandCursor)
    input_toolbar.addWidget(btn_next_input)

    input_toolbar.addStretch(1)
    stdin_history_label = QLabel("")
    stdin_history_label.setObjectName("ModeLabel")
    input_toolbar.addWidget(stdin_history_label)
    input_layout.addLayout(input_toolbar)

    stdin_edit = QPlainTextEdit()
    stdin_edit.setPlaceholderText("粘贴或输入测试数据…")
    input_layout.addWidget(stdin_edit, 1)
    detail_split.addWidget(input_card)

    output_card = QWidget()
    output_layout = QVBoxLayout(output_card)
    output_layout.setContentsMargins(0, 0, 0, 0)
    output_layout.setSpacing(8)
    output_layout.addWidget(QLabel("运行结果（stdout / stderr）"))

    output_edit = QPlainTextEdit()
    output_edit.setReadOnly(True)
    output_layout.addWidget(output_edit, 1)
    detail_split.addWidget(output_card)

    detail_split.setStretchFactor(0, 2)
    detail_split.setStretchFactor(1, 3)
    detail_split.setSizes([420, 560])
    layout.addWidget(detail_split, 1)

    bottom = QHBoxLayout()
    bottom.setSpacing(10)

    btn_run = QPushButton("运行测试")
    btn_run.setObjectName("PrimaryBtn")
    btn_run.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_run)

    btn_copy_stdin = QPushButton("复制测试数据")
    btn_copy_stdin.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_copy_stdin)

    btn_copy = QPushButton("复制结果")
    btn_copy.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_copy)

    btn_copy_sample = QPushButton("复制样例")
    btn_copy_sample.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_copy_sample)

    btn_export_zip = QPushButton("导出ZIP")
    btn_export_zip.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_export_zip)

    btn_ai_fix = QPushButton("解答错误？交给AI修复")
    btn_ai_fix.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_ai_fix)

    btn_feedback_retry = QPushButton("反馈重试")
    btn_feedback_retry.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_feedback_retry)

    bottom.addStretch(1)

    btn_close = QPushButton("关闭")
    btn_close.setCursor(Qt.PointingHandCursor)
    bottom.addWidget(btn_close)

    layout.addLayout(bottom)

    return {
        "progress_label": progress_label,
        "btn_prev": btn_prev,
        "btn_next": btn_next,
        "case_edit": case_edit,
        "btn_prev_input": btn_prev_input,
        "btn_next_input": btn_next_input,
        "stdin_history_label": stdin_history_label,
        "stdin_edit": stdin_edit,
        "output_edit": output_edit,
        "btn_run": btn_run,
        "btn_copy_stdin": btn_copy_stdin,
        "btn_copy": btn_copy,
        "btn_copy_sample": btn_copy_sample,
        "btn_export_zip": btn_export_zip,
        "btn_ai_fix": btn_ai_fix,
        "btn_feedback_retry": btn_feedback_retry,
        "btn_close": btn_close,
    }
