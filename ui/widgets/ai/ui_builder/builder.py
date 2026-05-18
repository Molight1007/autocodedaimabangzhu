"""UI 构建模块。

核心职责：
- 将 DeepSeekRefineDialog 的界面搭建逻辑集中到独立函数。
- 创建所有子控件并挂载到 dialog 实例属性上。
- 连接纯 UI 交互信号（模式切换、布局切换、帮助弹窗等）。

不负责：
- 不初始化 dialog 属性（_task_name / _chat_session 等）。
- 不创建 Handler 实例（StreamRenderHandler / AutonomousFixHandler）。
- 不连接业务信号（send_request / _cancel_current_request / _configure_key 等，由 Dialog 自行连接）。
"""

from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...code_editor.code_editor_container import CodeEditorContainer


def build_dialog_ui(dialog) -> None:
    """构建 DeepSeekRefineDialog 的全部界面控件。

    所有子控件作为 dialog 的属性挂载，信号连接到 dialog 的方法。
    """
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(12)

    _build_header(layout, dialog)
    _build_toolbar(layout, dialog)
    _build_input_card(layout, dialog)
    _build_splitter(layout, dialog)
    _build_button_box(layout, dialog)

    QTimer.singleShot(80, lambda: dialog.extra_input.setFocus())
    QTimer.singleShot(180, lambda: dialog.refresh_models(silent=True))


def _build_header(layout: QVBoxLayout, dialog) -> None:
    header = QWidget()
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(0, 0, 0, 0)
    header_layout.setSpacing(2)

    title_label = QLabel(dialog.windowTitle())
    title_label.setObjectName("DialogTitle")
    header_layout.addWidget(title_label)

    hint_label = QLabel("填写补充信息后发送请求，流式回显精炼结果。")
    hint_label.setObjectName("DialogHint")
    header_layout.addWidget(hint_label)

    layout.addWidget(header)


def _build_toolbar(layout: QVBoxLayout, dialog) -> None:
    from services.ai.deepseek_config import get_deepseek_model

    toolbar = QWidget()
    toolbar.setObjectName("Toolbar")
    toolbar_layout = QHBoxLayout(toolbar)
    toolbar_layout.setContentsMargins(10, 8, 10, 8)
    toolbar_layout.setSpacing(10)

    dialog.model_combo = QComboBox()
    dialog.model_combo.setMinimumWidth(200)
    dialog.model_combo.addItem(get_deepseek_model())
    dialog.model_combo.setCurrentText(get_deepseek_model())
    dialog.model_combo.currentTextChanged.connect(dialog._on_model_changed)
    toolbar_layout.addWidget(dialog.model_combo)

    dialog.btn_refresh_models = QPushButton("刷新模型")
    dialog.btn_refresh_models.setCursor(Qt.PointingHandCursor)
    dialog.btn_refresh_models.clicked.connect(dialog.refresh_models)
    toolbar_layout.addWidget(dialog.btn_refresh_models)

    dialog.status_badge = QLabel("就绪")
    dialog.status_badge.setObjectName("StatusBadge")
    toolbar_layout.addWidget(dialog.status_badge)

    toolbar_layout.addStretch(1)

    dialog.btn_config_key = QPushButton("配置 Key")
    dialog.btn_config_key.setCursor(Qt.PointingHandCursor)
    dialog.btn_config_key.clicked.connect(dialog._configure_key)
    toolbar_layout.addWidget(dialog.btn_config_key)

    dialog.btn_toggle_layout = QPushButton("切换上下")
    dialog.btn_toggle_layout.setCursor(Qt.PointingHandCursor)
    dialog.btn_toggle_layout.clicked.connect(dialog._toggle_splitter_orientation)
    toolbar_layout.addWidget(dialog.btn_toggle_layout)

    dialog.mode_combo = QComboBox()
    dialog.mode_combo.addItem("普通请求")
    dialog.mode_combo.addItem("自治修复")
    dialog.mode_combo.setFixedWidth(90)
    dialog.mode_combo.currentTextChanged.connect(dialog._on_mode_changed)
    toolbar_layout.addWidget(dialog.mode_combo)

    dialog.cross_validation_checkbox = QCheckBox("双算法验证")
    dialog.cross_validation_checkbox.setCursor(Qt.PointingHandCursor)
    dialog.cross_validation_checkbox.setVisible(False)
    toolbar_layout.addWidget(dialog.cross_validation_checkbox)

    dialog.btn_mode_help = QLabel("<a href='#' style='color:#4a90d9;text-decoration:none;'>?</a>")
    dialog.btn_mode_help.setCursor(Qt.PointingHandCursor)
    dialog.btn_mode_help.setVisible(False)
    dialog.btn_mode_help.linkActivated.connect(dialog._show_mode_help)
    toolbar_layout.addWidget(dialog.btn_mode_help)

    dialog.btn_send = QPushButton("发送请求")
    dialog.btn_send.setObjectName("PrimaryBtn")
    dialog.btn_send.setCursor(Qt.PointingHandCursor)
    dialog.btn_send.clicked.connect(dialog.send_request)
    toolbar_layout.addWidget(dialog.btn_send)

    dialog.btn_cancel = QPushButton("取消")
    dialog.btn_cancel.setCursor(Qt.PointingHandCursor)
    dialog.btn_cancel.setVisible(False)
    dialog.btn_cancel.clicked.connect(dialog._cancel_current_request)
    toolbar_layout.addWidget(dialog.btn_cancel)

    layout.addWidget(toolbar)


def _build_input_card(layout: QVBoxLayout, dialog) -> None:
    input_card = QWidget()
    input_card.setObjectName("PaneCard")
    input_layout = QVBoxLayout(input_card)
    input_layout.setContentsMargins(10, 10, 10, 10)
    input_layout.setSpacing(8)

    problem_title = QLabel("题目描述 / 约束（必填）")
    problem_title.setObjectName("PaneTitle")
    input_layout.addWidget(problem_title)

    dialog.problem_input = QPlainTextEdit()
    dialog.problem_input.setPlaceholderText("粘贴题目描述、输入输出格式、约束范围等（必填，至少 20 字）…")
    dialog.problem_input.setFixedHeight(90)
    input_layout.addWidget(dialog.problem_input)

    extra_title = QLabel("补充要求（可选）")
    extra_title.setObjectName("PaneTitle")
    input_layout.addWidget(extra_title)

    dialog.extra_input = QPlainTextEdit()
    dialog.extra_input.setPlaceholderText("例如：保留函数签名、修复边界、优化可读性、保持返回格式不变…")
    dialog.extra_input.setFixedHeight(80)
    input_layout.addWidget(dialog.extra_input)

    score_title = QLabel("评分范围（可选）")
    score_title.setObjectName("PaneTitle")
    input_layout.addWidget(score_title)

    score_row = QHBoxLayout()
    score_row.setSpacing(8)

    dialog.score_min_input = QLineEdit()
    dialog.score_min_input.setFixedWidth(90)
    dialog.score_min_input.setAlignment(Qt.AlignCenter)
    dialog.score_min_input.setPlaceholderText("最低分")
    score_row.addWidget(dialog.score_min_input)

    score_row.addWidget(QLabel("到"))

    dialog.score_max_input = QLineEdit()
    dialog.score_max_input.setFixedWidth(90)
    dialog.score_max_input.setAlignment(Qt.AlignCenter)
    dialog.score_max_input.setPlaceholderText("最高分")
    score_row.addWidget(dialog.score_max_input)

    score_row.addStretch(1)
    input_layout.addLayout(score_row)

    layout.addWidget(input_card)


def _build_splitter(layout: QVBoxLayout, dialog) -> None:
    dialog.splitter = QSplitter(Qt.Horizontal)
    dialog.splitter.setChildrenCollapsible(False)

    left_pane = _build_pane("源代码（含思考，流式回显）", read_only=True, parent=dialog)
    right_pane = _build_pane("精炼代码（可覆盖使用）", read_only=False, parent=dialog)

    dialog.left_editor: CodeEditorContainer = left_pane["editor"]
    dialog.right_editor: CodeEditorContainer = right_pane["editor"]

    dialog.left_editor.disable_completer()
    dialog.right_editor.disable_completer()
    dialog.left_editor.disable_highlighter()
    dialog.left_editor.disable_syntax_error_highlighter()
    dialog.left_editor.disable_find_replace()
    dialog.right_editor.disable_find_replace()

    dialog.left_editor.set_text("")
    dialog.right_editor.set_text("")

    dialog.splitter.addWidget(left_pane["widget"])
    dialog.splitter.addWidget(right_pane["widget"])
    dialog.splitter.setStretchFactor(0, 1)
    dialog.splitter.setStretchFactor(1, 1)
    dialog.splitter.setSizes([580, 580])

    layout.addWidget(dialog.splitter, 1)


def _build_button_box(layout: QVBoxLayout, dialog) -> None:
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    dialog.btn_apply = buttons.button(QDialogButtonBox.Ok)
    dialog.btn_apply.setText("应用覆盖")
    dialog.btn_apply.setObjectName("PrimaryBtn")
    buttons.accepted.connect(dialog._on_apply)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)


def _build_pane(title: str, *, read_only: bool, parent=None) -> Dict[str, Any]:
    pane = QWidget()
    pane.setObjectName("PaneCard")
    pane_layout = QVBoxLayout(pane)
    pane_layout.setContentsMargins(10, 10, 10, 10)
    pane_layout.setSpacing(8)

    pane_title = QLabel(title)
    pane_title.setObjectName("PaneTitle")
    pane_layout.addWidget(pane_title)

    editor = CodeEditorContainer(parent)
    editor.editor.setReadOnly(bool(read_only))
    pane_layout.addWidget(editor, 1)

    return {"widget": pane, "editor": editor}
