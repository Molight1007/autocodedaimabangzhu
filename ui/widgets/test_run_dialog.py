"""
测试运行对话框模块

核心功能：
1. 自动批量执行示例测试用例，展示每条用例的运行结果（stdout/stderr/超时状态）
2. 支持用户手动输入测试数据进行单次运行
3. 结果缓存机制：相同代码+用例组合的执行结果会被缓存，避免重复执行
4. AI 辅助修复：将失败的测试结果回传给外部 AI 服务进行代码修复

架构说明：
- TestRunDialog: 对话框主类，负责 UI 交互和状态管理
- TestRunController: 管理测试运行状态列表（runs），处理状态转换和缓存读写
- SolveBatchRunner: 子进程执行器，通过 QProcess 在独立进程中运行 solve() 函数
- ResultCache: 基于 (code_hash, case_hash) 的 LRU 缓存，存储历史执行结果
- StdinHistoryController: 管理用户手动输入的测试数据历史，支持前后导航

执行流程：
1. 对话框打开 -> _auto_run_examples() -> 启动 _batch_runner 批量执行所有用例
2. 每条用例执行完成 -> case_finished 信号 -> 更新 runs 列表和缓存 -> 刷新 UI
3. 用户手动运行 -> _run_single_clicked() -> 启动 _single_runner 执行单条用例
4. AI 修复 -> feedback_requested 信号 -> 外部监听者处理（如 DeepSeekRefineDialog）

数据流：
problem_set (题集数据) -> cases (测试用例列表) -> runs (运行状态列表) -> UI 展示
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
)

from services.solve_batch_runner import SolveBatchRunner
from services.test_zip_exporter import export_tests_zip, get_output_dir
from utils.ai_text_utils import clip_text
from .test_run import (
    TestRunController,
    build_test_run_dialog_ui,
    build_feedback_payload,
    collect_feedback_input,
    format_case_preview,
    format_output_text,
    ResultCache,
    StdinHistoryController,
)
from ..styles.code_editor_dialog_style import CODE_EDITOR_DIALOG_QSS

_DEFAULT_VENV_PYTHON = r"I:\.PythonVenv\.PyQtApp\Scripts\python.exe"
_SETTINGS_ORG = "PyQt5DataGenerator"
_SETTINGS_APP = "DataGenerator"
_STDIN_HISTORY_KEY = "test_run_dialog/stdin_history"
_STDIN_HISTORY_MAX = 50


def _sanitize_zip_basename(text: str) -> str:
    raw = str(text or "").strip().strip(". ")
    if not raw:
        return ""
    bad_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad_chars else ch for ch in raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:120].strip(" ._")


def _default_zip_name() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"tests_{ts}.zip"


class TestRunDialog(QDialog):
    """测试运行窗口，负责展示、调度与 feedback_requested 信号发射。"""
    feedback_requested = Signal(dict)

    def __init__(self, parent=None, *, problem_set: Dict[str, Any], code: str, ai_session_ready: bool = False):
        super().__init__(parent)
        self.setWindowTitle("测试运行")
        self.setModal(True)
        self.resize(980, 680)
        self.setStyleSheet(CODE_EDITOR_DIALOG_QSS)

        self._problem_set = dict(problem_set or {})
        self._cases: List[Dict[str, Any]] = list(self._problem_set.get("cases") or [])
        self._code = code or ""
        self._timeout_sec = 3.0
        self._result_cache = ResultCache(max_items=2000)

        self._run_controller = TestRunController(
            cases=self._cases,
            cache_get=self._result_cache.get,
            cache_put=self._result_cache.put,
            make_cache_key=self._result_cache.make_key,
        )
        self._runs = self._run_controller.runs
        self._display_index = 0

        self._batch_runner = SolveBatchRunner(self)
        self._single_runner = SolveBatchRunner(self)

        self._stdin_history_controller = StdinHistoryController(max_items=_STDIN_HISTORY_MAX)
        self._cleaned: bool = False
        self._ai_session_ready = bool(ai_session_ready)

        ui = build_test_run_dialog_ui(self, case_count=len(self._cases))
        self.progress_label = ui["progress_label"]
        self.btn_prev = ui["btn_prev"]
        self.btn_next = ui["btn_next"]
        self.case_edit = ui["case_edit"]
        self.btn_prev_input = ui["btn_prev_input"]
        self.btn_next_input = ui["btn_next_input"]
        self.stdin_history_label = ui["stdin_history_label"]
        self.stdin_edit = ui["stdin_edit"]
        self.output_edit = ui["output_edit"]
        self.btn_run = ui["btn_run"]
        self.btn_copy_stdin = ui["btn_copy_stdin"]
        self.btn_copy = ui["btn_copy"]
        self.btn_copy_sample = ui["btn_copy_sample"]
        self.btn_export_zip = ui["btn_export_zip"]
        self.btn_ai_fix = ui["btn_ai_fix"]
        self.btn_feedback_retry = ui["btn_feedback_retry"]
        self.btn_close = ui["btn_close"]

        self.btn_prev.clicked.connect(self._select_prev)
        self.btn_next.clicked.connect(self._select_next)
        self.btn_prev_input.clicked.connect(self._stdin_prev)
        self.btn_next_input.clicked.connect(self._stdin_next)
        self.btn_run.clicked.connect(self._run_single_clicked)
        self.btn_copy_stdin.clicked.connect(self._on_copy_stdin_clicked)
        self.btn_copy.clicked.connect(self._on_copy_clicked)
        self.btn_copy_sample.clicked.connect(self._on_copy_sample_clicked)
        self.btn_export_zip.clicked.connect(self._on_export_zip_clicked)
        self.btn_ai_fix.clicked.connect(self._on_ai_fix_clicked)
        self.btn_feedback_retry.clicked.connect(self._on_feedback_retry_clicked)
        self.btn_close.clicked.connect(self.reject)

        self._sync_ai_buttons()

        self._batch_runner.case_started.connect(self._on_batch_case_started)
        self._batch_runner.case_finished.connect(self._on_batch_case_finished)
        self._batch_runner.all_finished.connect(self._on_batch_all_finished)
        self._batch_runner.running_changed.connect(self._on_batch_running_changed)

        self._single_runner.case_finished.connect(self._on_single_case_finished)

        QTimer.singleShot(0, self._refresh_ui)
        QTimer.singleShot(0, self._auto_run_examples)

    def set_code(self, code: str, *, rerun_examples: bool = False) -> None:
        self._code = code or ""
        if bool(rerun_examples):
            self._auto_run_examples()

    def _pick_python_exe(self) -> str:
        p = _DEFAULT_VENV_PYTHON
        if p and os.path.exists(p):
            return p
        return sys.executable

    def _get_run(self, idx: int) -> Optional[Dict[str, Any]]:
        return self._run_controller.get_run(idx)

    def _refresh_ui(self) -> None:
        self._update_progress_label()
        self._update_nav_buttons()
        self._update_stdin_history_ui()
        self._show_index(self._display_index)

    def _update_progress_label(self) -> None:
        total = len(self._runs)
        if total <= 0:
            self.progress_label.setText("0/0")
            return

        if self._batch_runner.is_running():
            shown = min(total, max(1, self._run_controller.max_ready_index + 1))
            self.progress_label.setText(f"{shown}/{total}")
            return

        done = self._run_controller.count_done()
        self.progress_label.setText(f"{done}/{total}")

    def _update_nav_buttons(self) -> None:
        """下一个按钮仅允许导航到已完成范围（max_ready_index）。"""
        if not self._runs:
            self.btn_prev.setEnabled(False)
            self.btn_next.setEnabled(False)
            return
        self.btn_prev.setEnabled(self._display_index > 0)
        upper = min(self._run_controller.max_ready_index, len(self._runs) - 1)
        self.btn_next.setEnabled(self._display_index < upper)

    def _show_index(self, idx: int) -> None:
        r = self._get_run(idx)
        if not r:
            self.case_edit.setPlainText("")
            self.stdin_edit.setPlainText("")
            self.output_edit.setPlainText("")
            return

        case = dict(r.get("case") or {})
        self.case_edit.setPlainText(format_case_preview(case))

        stdin_text = str(case.get("stdin") or "")
        if self.stdin_edit.toPlainText() != stdin_text:
            self.stdin_edit.setPlainText(stdin_text)
            cursor = self.stdin_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.stdin_edit.setTextCursor(cursor)

        shown = format_output_text(
            stdout=str(r.get("stdout") or ""),
            stderr=str(r.get("stderr") or ""),
            timed_out=bool(r.get("timed_out")),
            timeout_sec=self._timeout_sec,
        )
        self.output_edit.setPlainText(shown)

    def _select_prev(self) -> None:
        self._display_index = max(0, self._display_index - 1)
        self._refresh_ui()

    def _select_next(self) -> None:
        upper = min(self._run_controller.max_ready_index, len(self._runs) - 1)
        self._display_index = min(upper, self._display_index + 1)
        self._refresh_ui()

    def _auto_run_examples(self) -> None:
        """
        自动执行所有示例用例
        
        流程：
        1. 检查代码和用例是否有效
        2. 停止单次运行器（避免冲突）
        3. 重置所有用例状态为 Pending
        4. 收集缓存结果（相同代码+用例可跳过执行）
        5. 启动批量运行器
        
        SolveBatchRunner 会为每个用例创建子进程执行 solve() 函数，
        通过 case_finished 信号返回执行结果
        """
        if not (self._code or "").strip():
            self._refresh_ui()
            return
        if not self._cases:
            self._refresh_ui()
            return
        if self._batch_runner.is_running():
            return

        self._single_runner.stop()
        self._run_controller.reset_examples_state()
        cached_results = self._run_controller.collect_cached_results(code=self._code)

        self._display_index = 0
        self._refresh_ui()

        self._batch_runner.start(
            solve_code=self._code,
            cases=self._cases,
            timeout_sec=self._timeout_sec,
            python_exe=self._pick_python_exe(),
            cached_results=cached_results,
        )

    def _on_batch_running_changed(self, running: bool) -> None:
        _ = running
        self._refresh_ui()

    def _on_batch_case_started(self, row_idx: int) -> None:
        if not self._run_controller.on_batch_case_started(int(row_idx)):
            return
        self._refresh_ui()

    def _on_batch_case_finished(self, row_idx: int, payload: Dict[str, Any]) -> None:
        """回写单条结果到控制器，由控制器统一判定状态并写缓存。"""
        ok = self._run_controller.on_batch_case_finished(int(row_idx), payload, code=self._code)
        if not ok:
            return
        self._refresh_ui()

    def _on_batch_all_finished(self) -> None:
        self._run_controller.on_batch_all_finished()
        self._refresh_ui()

    def _run_single_clicked(self) -> None:
        stdin_text = self.stdin_edit.toPlainText()
        if not (self._code or "").strip():
            QMessageBox.information(self, "提示", "请先填写可运行的 solve 代码。")
            return
        if not stdin_text.strip():
            QMessageBox.information(self, "提示", "请输入测试数据。")
            return
        if self._single_runner.is_running():
            return

        self._push_stdin_history(stdin_text)

        cached = self._run_controller.get_single_cached_result(code=self._code, stdin_text=stdin_text)
        if cached:
            self._on_single_case_finished(0, cached)
            return

        self._single_runner.start(
            solve_code=self._code,
            cases=[{"stdin": stdin_text}],
            timeout_sec=self._timeout_sec,
            python_exe=self._pick_python_exe(),
        )

    def _on_single_case_finished(self, row_idx: int, payload: Dict[str, Any]) -> None:
        _ = row_idx
        text = format_output_text(
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            timed_out=bool(payload.get("timed_out")),
            timeout_sec=self._timeout_sec,
        )
        self.output_edit.setPlainText(text)

        stdin_text = self.stdin_edit.toPlainText()
        self._run_controller.put_single_result(code=self._code, stdin_text=stdin_text, payload=payload)

        self._single_runner.stop()

    def _on_copy_clicked(self) -> None:
        text = self.output_edit.toPlainText()
        QApplication.clipboard().setText(text or "")

    def _on_copy_stdin_clicked(self) -> None:
        text = self.stdin_edit.toPlainText()
        QApplication.clipboard().setText(text or "")

    def _on_copy_sample_clicked(self) -> None:
        stdin_text = self.stdin_edit.toPlainText()
        out_text = self.output_edit.toPlainText()
        parts: List[str] = []
        if (stdin_text or "").strip():
            parts.append((stdin_text or "").rstrip() + "\n")
        if (out_text or "").strip():
            parts.append((out_text or "").rstrip() + "\n")
        QApplication.clipboard().setText(("\n".join(parts)).rstrip() + ("\n" if parts else ""))

    def _on_export_zip_clicked(self) -> None:
        out_dir = str(get_output_dir() or "").strip() or os.path.expanduser("~")
        zip_basename = self._resolve_export_zip_basename()
        export_dir = out_dir

        try:
            if not zip_basename:
                default_path = os.path.join(out_dir, _default_zip_name())
                selected_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "导出 ZIP",
                    default_path,
                    "ZIP 文件 (*.zip)",
                )
                selected = str(selected_path or "").strip()
                if not selected:
                    return
                export_dir = os.path.dirname(selected) or out_dir
                zip_basename = os.path.basename(selected)

            zip_path = export_tests_zip(
                output_dir=export_dir,
                cases=list(self._problem_set.get("cases") or []),
                run_payloads=list(self._runs or []),
                extra_files=self._collect_export_extra_files(),
                zip_basename=zip_basename,
            )
        except Exception as e:
            QMessageBox.information(self, "提示", f"导出失败：{e}")
            return

        QMessageBox.information(self, "提示", f"已导出：\n{zip_path}")

    def _collect_export_extra_files(self) -> Dict[str, str]:
        extra: Dict[str, str] = {}
        solve_code = str(self._code or "").strip()
        if solve_code:
            extra["solve.py"] = solve_code if solve_code.endswith("\n") else solve_code + "\n"

        generate_data_code = str(self._problem_set.get("generate_data_code") or "").strip()
        if not generate_data_code:
            for item in list(self._problem_set.get("cases") or []):
                case = dict(item or {})
                meta = case.get("meta")
                if not isinstance(meta, dict):
                    continue
                code = str(meta.get("generate_data_code") or "").strip()
                if code:
                    generate_data_code = code
                    break
        if generate_data_code:
            extra["generate_data.py"] = (
                generate_data_code if generate_data_code.endswith("\n") else generate_data_code + "\n"
            )
        return extra

    def _resolve_export_zip_basename(self) -> str:
        title = _sanitize_zip_basename(self._problem_set.get("title"))
        if title:
            return title
        for item in list(self._problem_set.get("cases") or []):
            case = dict(item or {})
            title = _sanitize_zip_basename(case.get("title"))
            if title:
                return title
            meta = case.get("meta")
            if isinstance(meta, dict):
                title = _sanitize_zip_basename(meta.get("title"))
                if title:
                    return title
        return ""

    def _on_feedback_retry_clicked(self) -> None:
        self._emit_ai_request_payload(action="feedback_retry")

    def _on_ai_fix_clicked(self) -> None:
        self._emit_ai_request_payload(action="start_ai_fix")

    def _emit_ai_request_payload(self, *, action: str) -> None:
        """保持 feedback_requested 协议稳定，只在此处组装 AI 请求负载。"""
        extra = collect_feedback_input(self, current_stdin=self.stdin_edit.toPlainText())
        if extra is None:
            return
        idx = int(self._display_index)
        run = dict(self._run_controller.get_run(idx) or {})
        payload = build_feedback_payload(action=action, index=idx, run=run, extra=extra, clip_text=clip_text)
        self.feedback_requested.emit(payload)
        self._ai_session_ready = True
        self._sync_ai_buttons()
        if str(action or "") == "start_ai_fix":
            QMessageBox.information(self, "提示", "已回传当前测试结果，正在打开 DeepSeek 进行首次修复。")
        else:
            QMessageBox.information(self, "提示", "已回传当前测试结果，正在打开 DeepSeek 进行反馈重试。")

    def _sync_ai_buttons(self) -> None:
        ready = bool(self._ai_session_ready)
        self.btn_ai_fix.setVisible(not ready)
        self.btn_feedback_retry.setVisible(ready)

    def _push_stdin_history(self, text: str) -> None:
        self._stdin_history_controller.push(text)
        self._update_stdin_history_ui()

    def _update_stdin_history_ui(self) -> None:
        label_text, can_prev, can_next = self._stdin_history_controller.ui_state()
        self.stdin_history_label.setText(label_text)
        self.btn_prev_input.setEnabled(can_prev)
        self.btn_next_input.setEnabled(can_next)

    def _stdin_prev(self) -> None:
        text = self._stdin_history_controller.prev_text()
        if text is None:
            return
        self.stdin_edit.setPlainText(text)
        cursor = self.stdin_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.stdin_edit.setTextCursor(cursor)
        self._update_stdin_history_ui()

    def _stdin_next(self) -> None:
        text = self._stdin_history_controller.next_text()
        if text is None:
            return
        self.stdin_edit.setPlainText(text)
        cursor = self.stdin_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.stdin_edit.setTextCursor(cursor)
        self._update_stdin_history_ui()

    def _cleanup(self) -> None:
        """
        清理资源
        
        对话框关闭时调用，确保：
        1. 停止所有运行中的子进程
        2. 清空结果缓存
        3. 清空输入历史
        4. 清空 UI 控件内容
        """
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self._batch_runner.stop()
        except Exception:
            pass
        try:
            self._single_runner.stop()
        except Exception:
            pass
        self._result_cache.clear()
        try:
            s = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
            s.remove(_STDIN_HISTORY_KEY)
            s.sync()
        except Exception:
            pass
        self._stdin_history_controller.clear()
        try:
            self.case_edit.setPlainText("")
            self.stdin_edit.setPlainText("")
            self.output_edit.setPlainText("")
            self._update_stdin_history_ui()
        except Exception:
            pass

    def done(self, r: int) -> None:
        self._cleanup()
        super().done(r)

    def closeEvent(self, event) -> None:
        self._cleanup()
        super().closeEvent(event)
