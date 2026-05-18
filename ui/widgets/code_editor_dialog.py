from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QDialogButtonBox, QWidget, QLabel, QHBoxLayout,
    QPushButton, QMessageBox
)

from .code_editor.code_editor_container import CodeEditorContainer
from ..styles.code_editor_dialog_style import CODE_EDITOR_DIALOG_QSS
from services.prompts.code_templates import get_template
from services.prompts.prompt_builders import build_generate_data_prompt, build_solve_prompt
from services.ai.default_dispatch import get_default_dispatch_api
from ui.widgets.ai.deepseek_refine_dialog import DeepSeekRefineDialog


class CodeEditorDialog(QDialog):
    def __init__(self, parent=None, title: str = "编辑代码"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(900, 600)
        self.setStyleSheet(CODE_EDITOR_DIALOG_QSS)

        # 设置焦点策略，允许补全弹窗获取焦点
        self.setFocusPolicy(Qt.StrongFocus)

        self._mode = "generate_data"
        self._problem: Dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        header = QWidget()
        header.setObjectName("DialogHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        title_col = QWidget()
        title_col_layout = QVBoxLayout(title_col)
        title_col_layout.setContentsMargins(0, 0, 0, 0)
        title_col_layout.setSpacing(2)

        title_label = QLabel(self.windowTitle())
        title_label.setObjectName("DialogTitle")
        title_col_layout.addWidget(title_label)

        hint_label = QLabel("支持自动缩进与语法高亮。使用模板生成可执行函数。")
        hint_label.setObjectName("DialogHint")
        title_col_layout.addWidget(hint_label)

        header_layout.addWidget(title_col, 1)
        layout.addWidget(header)

        toolbar = QWidget()
        toolbar.setObjectName("Toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(10)

        self.mode_label = QLabel("")
        self.mode_label.setObjectName("ModeLabel")
        toolbar_layout.addWidget(self.mode_label)

        toolbar_layout.addStretch(1)

        self.btn_reset_template = QPushButton("重置模板")
        self.btn_reset_template.setCursor(Qt.PointingHandCursor)
        self.btn_reset_template.clicked.connect(self.reset_template)
        toolbar_layout.addWidget(self.btn_reset_template)

        self.btn_ask_gpt = QPushButton("问问DeepSeek")
        self.btn_ask_gpt.setObjectName("PrimaryBtn")
        self.btn_ask_gpt.setCursor(Qt.PointingHandCursor)
        self.btn_ask_gpt.clicked.connect(self.ask_gpt)
        toolbar_layout.addWidget(self.btn_ask_gpt)

        layout.addWidget(toolbar)

        self.editor = CodeEditorContainer(self)
        self.editor.set_dispatch_api(get_default_dispatch_api())
        self.editor.enable_find_replace()
        layout.addWidget(self.editor, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_mode_label()

        # 确保编辑器获得焦点
        QTimer.singleShot(100, lambda: self.editor.editor.setFocus())

    def set_text(self, text: str):
        self.editor.set_text(text)

    def get_text(self) -> str:
        return self.editor.get_text()

    def set_problem(self, problem: Optional[Dict[str, Any]]):
        self._problem = problem or {}

    def set_mode(self, mode: str):
        mode = (mode or "").strip().lower()
        if mode not in {"generate_data", "solve"}:
            mode = "generate_data"
        self._mode = mode
        self._sync_mode_label()

    def get_mode(self) -> str:
        return self._mode

    def reset_template(self):
        self.set_text(get_template(self._mode))

    def ask_gpt(self):
        try:
            source = self.get_text()

            if self._mode == "generate_data":
                mode_constraint = (
                    "角色约束：你正在生成测试数据，非解题。函数签名为 generate_data(problem) -> dict，"
                    "stdout 键的值必须为空字符串 ''，禁止输出任何解题结果或计算答案。"
                )
            else:
                mode_constraint = (
                    "角色约束：你正在生成解题代码，函数签名为 solve(problem, data) -> dict，"
                    "需要根据 data['stdin'] 计算正确答案写入 stdout。"
                )

            extra = "\n".join(
                [
                    mode_constraint,
                    "请基于下方源代码进行精炼与纠错，输出可直接运行的 Python 代码。",
                    "要求：",
                    "1) 只输出代码，不要解释，不要 Markdown。",
                    "2) 保持核心函数签名与返回契约不变。",
                    "3) 禁止文件/系统/网络操作，禁止 eval/exec。",
                    "",
                    "源代码：",
                    "```python",
                    source,
                    "```",
                ]
            )

            if self._mode == "solve":
                spec = self._problem.get("spec") if isinstance(self._problem, dict) else self._problem
                stdin = ""
                if isinstance(self._problem, dict):
                    stdin = str(self._problem.get("stdin") or self._problem.get("input") or "")
                prompt = build_solve_prompt(spec or {}, stdin, extra=extra)
            else:
                prompt = build_generate_data_prompt(self._problem, extra=extra)

            dlg = DeepSeekRefineDialog(
                self,
                title="DeepSeek 精炼",
                task_name=self._mode,
                source_code=source,
                prompt=prompt,
                problem=self._problem,
            )
            if dlg.exec():
                self.set_text(dlg.get_refined_code())
        except Exception as e:
            QMessageBox.information(self, "提示", f"问问DeepSeek 暂不可用：{e}")

    def _sync_mode_label(self):
        if self._mode == "solve":
            self.mode_label.setText("解题（solve）")
        else:
            self.mode_label.setText("测试数据生成（generate_data）")
