from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from services.ai.default_dispatch import get_default_dispatch_api
from services.prompts.code_templates import get_template
from services.prompts.prompt_builders import build_solve_prompt
from ui.widgets.ai.deepseek_refine_dialog import DeepSeekRefineDialog
from utils.ai_text_utils import clip_text
from ..styles.code_editor_dialog_style import CODE_EDITOR_DIALOG_QSS
from .code_editor.code_editor_container import CodeEditorContainer
from .test_run_dialog import TestRunDialog


def _render_problem_set_text(problem_set: Dict[str, Any]) -> str:
    import json

    try:
        soft_limit_chars = 120_000
        hard_limit_chars = 320_000

        ps = dict(problem_set or {})
        cases = list(ps.get("cases") or [])

        def _dump(x: Any) -> str:
            return json.dumps(x, ensure_ascii=False, indent=2)

        if not cases:
            text = _dump(ps)
            if len(text) <= hard_limit_chars:
                return text + "\n"
            return text[:hard_limit_chars].rstrip() + "\n...\n"

        base = dict(ps)
        base.pop("cases", None)
        if "count" not in base:
            base["count"] = len(cases)

        preview_cases: List[Dict[str, Any]] = []
        preview = dict(base)
        preview["cases"] = preview_cases

        preview_cases.append(dict(cases[0] or {}))
        preview_text = _dump(preview)
        if len(preview_text) > hard_limit_chars:
            c0 = dict(cases[0] or {})
            stdin0 = c0.get("stdin")
            if isinstance(stdin0, str) and len(stdin0) > 80_000:
                c0["stdin"] = stdin0[:80_000] + "\n...\n"
            preview_cases[0] = c0
            preview_text = _dump(preview)
            if len(preview_text) > hard_limit_chars:
                preview_text = preview_text[:hard_limit_chars].rstrip() + "\n...\n"
            else:
                preview_text = preview_text.rstrip() + "\n...\n"
            preview_text += f"(truncated: showing 1/{len(cases)} cases)\n"
            return preview_text

        if len(preview_text) <= soft_limit_chars:
            full_text = _dump(ps)
            if len(full_text) <= hard_limit_chars:
                return full_text + "\n"

        for i in range(1, len(cases)):
            preview_cases.append(dict(cases[i] or {}))
            t = _dump(preview)
            if len(t) > hard_limit_chars:
                preview_cases.pop()
                break
            preview_text = t

        if len(preview_cases) >= len(cases):
            return preview_text + "\n"

        preview_text = preview_text.rstrip() + "\n...\n"
        preview_text += f"(truncated: showing {len(preview_cases)}/{len(cases)} cases)\n"
        return preview_text
    except Exception:
        return repr(problem_set) + "\n"


class ProblemWorkbenchDialog(QDialog):
    def __init__(self, parent=None, *, problem_set: Dict[str, Any]):
        super().__init__(parent)
        self.setWindowTitle("测试数据工作台")
        self.setModal(True)
        self.resize(1180, 780)
        self.setStyleSheet(CODE_EDITOR_DIALOG_QSS)

        self._problem_set: Dict[str, Any] = dict(problem_set or {})
        self._dispatch_api = get_default_dispatch_api()
        self._refine_dialog: Optional[DeepSeekRefineDialog] = None
        self._ai_session_ready = False
        self._manual_sample_stdin = ""
        self._manual_sample_stdout = ""

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

        title_label = QLabel("测试数据工作台")
        title_label.setObjectName("DialogTitle")
        title_col_layout.addWidget(title_label)

        hint_label = QLabel("编辑 solve 代码 → 测试运行。左侧展示生成的源数据。")
        hint_label.setObjectName("DialogHint")
        title_col_layout.addWidget(hint_label)

        header_layout.addWidget(title_col, 1)
        layout.addWidget(header)

        toolbar = QWidget()
        toolbar.setObjectName("Toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(10)

        self.mode_label = QLabel("解题（solve）")
        self.mode_label.setObjectName("ModeLabel")
        toolbar_layout.addWidget(self.mode_label)

        toolbar_layout.addStretch(1)

        self.btn_reset = QPushButton("重置模板")
        self.btn_reset.setCursor(Qt.PointingHandCursor)
        self.btn_reset.clicked.connect(self._reset_template)
        toolbar_layout.addWidget(self.btn_reset)

        self.btn_ask = QPushButton("问问 AI")
        self.btn_ask.setObjectName("PrimaryBtn")
        self.btn_ask.setCursor(Qt.PointingHandCursor)
        self.btn_ask.clicked.connect(self._ask_deepseek)
        toolbar_layout.addWidget(self.btn_ask)

        self.btn_test = QPushButton("测试运行")
        self.btn_test.setCursor(Qt.PointingHandCursor)
        self.btn_test.clicked.connect(self._open_test_run)
        toolbar_layout.addWidget(self.btn_test)

        layout.addWidget(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("源数据（题目字典）"))

        self.problem_view = QPlainTextEdit()
        self.problem_view.setReadOnly(True)
        self.problem_view.setPlainText(_render_problem_set_text(self._problem_set))
        left_layout.addWidget(self.problem_view, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(QLabel("代码编辑器（solve）"))

        self.editor = CodeEditorContainer(self)
        self.editor.set_dispatch_api(self._dispatch_api)
        self.editor.enable_find_replace()
        self.editor.set_text(get_template("solve"))
        right_layout.addWidget(self.editor, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 720])
        layout.addWidget(splitter, 1)

        QTimer.singleShot(100, lambda: self.editor.editor.setFocus())

    def _reset_template(self):
        self.editor.set_text(get_template("solve"))

    def _pick_shortest_case_sample(self) -> tuple[str, str]:
        cases: List[Dict[str, Any]] = list((self._problem_set or {}).get("cases") or [])
        shortest_stdin = ""
        shortest_stdout = ""
        found = False
        for item in cases:
            case = item if isinstance(item, dict) else {}
            stdin_text = str(case.get("stdin") or "")
            if not found or len(stdin_text) < len(shortest_stdin):
                shortest_stdin = stdin_text
                shortest_stdout = str(case.get("stdout") or "")
                found = True
        if not found:
            return "", ""
        return shortest_stdin, shortest_stdout

    def _collect_manual_sample(self) -> Optional[tuple[str, str]]:
        QMessageBox.information(
            self,
            "提示",
            "当前测试数据过大（最短样例超过 5000 字符）。\n请手动输入更小的样例输入/样例输出后再请求。",
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("输入精简样例")
        dlg.resize(820, 560)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel("样例输入（必填）"))
        stdin_edit = QPlainTextEdit()
        stdin_edit.setPlaceholderText("请输入可复现问题的精简样例输入")
        layout.addWidget(stdin_edit, 2)

        layout.addWidget(QLabel("样例输出（可选，建议填写期望输出）"))
        stdout_edit = QPlainTextEdit()
        stdout_edit.setPlaceholderText("例如：正确输出/目标输出")
        layout.addWidget(stdout_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return None
        sample_stdin = (stdin_edit.toPlainText() or "").strip()
        if not sample_stdin:
            QMessageBox.information(self, "提示", "样例输入不能为空。")
            return None
        sample_stdout = stdout_edit.toPlainText() or ""
        return sample_stdin, sample_stdout

    def _resolve_refine_sample(self) -> Optional[tuple[str, str]]:
        shortest_stdin, shortest_stdout = self._pick_shortest_case_sample()
        if shortest_stdin and len(shortest_stdin) <= 5_000:
            self._manual_sample_stdin = ""
            self._manual_sample_stdout = ""
            return shortest_stdin, shortest_stdout
        if self._manual_sample_stdin.strip():
            return self._manual_sample_stdin, self._manual_sample_stdout
        if not shortest_stdin:
            return "", ""
        manual = self._collect_manual_sample()
        if manual is None:
            return None
        self._manual_sample_stdin, self._manual_sample_stdout = manual
        return self._manual_sample_stdin, self._manual_sample_stdout

    def _build_refine_prompt(self, source: str, sample_stdin: str, sample_stdout: str) -> str:
        source_text = str(source or "").strip()

        # 截断过长的源码
        if len(source_text) > 120_000:
            source_text = clip_text(source_text, 80_000)

        # 构建 problem 规范
        problem_spec = {
            "stdin": sample_stdin,
            "stdout": sample_stdout,
        }

        # 附加源码作为补充要求（约束已在系统提示词中定义）
        extra = f"""下面是当前代码：
```python
{source_text}
```"""

        # 使用标准提示词构建器
        return build_solve_prompt(problem_spec, sample_stdin, extra=extra)

    def _ensure_refine_dialog(self) -> Optional[DeepSeekRefineDialog]:
        if self._refine_dialog is not None:
            return self._refine_dialog
        sample = self._resolve_refine_sample()
        if sample is None:
            return None
        sample_stdin, sample_stdout = sample
        source = self.editor.get_text()
        prompt = self._build_refine_prompt(source, sample_stdin, sample_stdout)
        self._refine_dialog = DeepSeekRefineDialog(
            self,
            title="AI 精炼",
            task_name="solve",
            source_code=source,
            prompt=prompt,
            problem={"stdin": sample_stdin, "stdout": sample_stdout},
            all_test_cases=list(self._problem_set.get("cases") or []),
        )
        return self._refine_dialog

    def _ask_deepseek(self):
        try:
            self._refine_dialog = None
            dlg = self._ensure_refine_dialog()
            if dlg is None:
                return
            if dlg.exec():
                self.editor.set_text(dlg.get_refined_code())
                self._ai_session_ready = True
        except Exception as e:
            QMessageBox.information(self, "提示", f"问问 AI 暂不可用：{e}")

    def _open_test_run(self):
        dlg = TestRunDialog(
            self,
            problem_set=self._problem_set,
            code=self.editor.get_text(),
            ai_session_ready=self._ai_session_ready,
        )
        dlg.feedback_requested.connect(self._on_feedback_retry_requested)
        dlg.exec()

    def _sync_sender_code(self, code: str) -> None:
        sender_obj = self.sender()
        if sender_obj is None:
            return
        if not hasattr(sender_obj, "set_code"):
            return
        try:
            sender_obj.set_code(code or "", rerun_examples=True)
        except Exception:
            return

    def _on_feedback_retry_requested(self, payload: Dict[str, Any]) -> None:
        try:
            action = str((payload or {}).get("action") or "feedback_retry").strip().lower()
            feedback_stdin = str((payload or {}).get("feedback_stdin") or "")
            feedback_expected = str((payload or {}).get("feedback_expected") or "")
            feedback_note = str((payload or {}).get("feedback_note") or "")
            status_text = str((payload or {}).get("status") or "")
            timed_out = bool((payload or {}).get("timed_out"))

            use_stdin = clip_text(feedback_stdin.strip(), 3_000)
            expected_part = clip_text(feedback_expected.strip(), 3_000)
            if not expected_part:
                expected_part = "请按题意给出正确输出，并保持返回格式与换行约束。"
            feedback_problem = "\n".join(
                [
                    "测试结果不正确，请基于前面对话继续修复。",
                    f"状态：{status_text or '未知'}，超时：{timed_out}",
                    "",
                    "用于复现的 stdin：",
                    use_stdin if use_stdin.strip() else "<empty>",
                    "",
                    "期望输出或目标：",
                    expected_part,
                    "",
                    "错误说明：",
                    clip_text(feedback_note.strip(), 3_000),
                ]
            )
            feedback_extra = "\n".join(
                [
                    "请只做必要改动修复错误。",
                    "保持已有变量命名、数据结构与整体实现框架，不要重置重写。",
                    "继续遵守：禁止文件/系统/网络操作，禁止 eval/exec。",
                ]
            )

            dlg = self._ensure_refine_dialog()
            if dlg is None:
                return
            if action == "feedback_retry" and not self._ai_session_ready:
                QMessageBox.information(self, "提示", "当前还没有 AI 会话，将自动按“首次修复”处理。")
            dlg.queue_followup_request(feedback_problem, feedback_extra)
            self._ai_session_ready = True
            if dlg.exec():
                new_code = dlg.get_refined_code()
                self.editor.set_text(new_code)
                self._sync_sender_code(new_code)
                self._ai_session_ready = True
        except Exception as e:
            QMessageBox.information(self, "提示", f"反馈重试失败：{e}")
