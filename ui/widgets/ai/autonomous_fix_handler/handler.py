"""自治修复适配层 Handler。

核心职责：
- 封装 AutonomousFixWorker 的创建、信号连接、回调处理全流程。
- 直接管理 left_editor（日志追加）、right_editor（代码更新）、status_badge（进度状态）。
- 通过 finished / error_occurred 信号通知 Dialog 恢复 UI 忙闲状态和弹窗。

不负责：
- 不管理 Dialog 整体忙闲状态（_set_busy 由 Dialog 在 finished 信号中处理）。
- 不弹 QMessageBox（error_occurred 信号由 Dialog 接收后处理）。
- 不决定候选代码来源（由调用方在 start 时传入）。

主要数据流：
1. 入口：Dialog 调用 start(problem, candidate_code, model, api_key, base_url)。
2. Handler 创建 llm_caller + AutonomousFixWorker，连接 6 个 Signal。
3. Worker 运行中：progress/step/code_update Signal → Handler 直接更新编辑器和状态标签。
4. Worker 完成：done Signal → Handler 更新编辑器 + 状态标签。
5. Worker 错误：err Signal → Handler 发射 error_occurred 信号。
6. Worker 线程结束：finished Signal → Handler 发射 finished 信号。

约束：
- cancel 使用协作取消（requestInterruption + wait），禁止 terminate。
- action_names/type_names 映射集中在本模块，避免与 AutonomousFixWorker 重复散落。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor

from services.ai.autonomous_fix_worker import AutonomousFixWorker
from services.ai.openai_compat_http import OpenAICompatHTTPClient

logger = logging.getLogger(__name__)

_ACTION_NAMES = {
    "read": "读取代码/题目",
    "run": "运行测试",
    "modify": "修改代码",
    "self_test": "自测验证",
    "finalize": "提交结果",
}

_TYPE_NAMES = {
    "right": "✓ 通过",
    "wrong": "✗ 错误",
    "runtime_error": "✗ 运行异常",
    "protocol_error": "✗ 协议错误",
    "finalize_ok": "✓ 完成",
}


class AutonomousFixHandler(QObject):
    """自治修复适配层，隔离 Worker 回调与 Dialog UI 更新。"""

    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, *, left_editor, right_editor, status_badge, parent=None):
        super().__init__(parent)
        self._left_editor = left_editor
        self._right_editor = right_editor
        self._status_badge = status_badge
        self._worker: Optional[AutonomousFixWorker] = None

    @property
    def worker(self) -> Optional[AutonomousFixWorker]:
        return self._worker

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def start(
        self,
        *,
        problem: Dict[str, Any],
        candidate_code: str,
        model: str,
        api_key: str,
        base_url: str,
        runtime_context: Optional[Dict[str, Any]] = None,
        enable_cross_validation: bool = False,
        all_test_cases: Optional[list[dict]] = None,
    ) -> None:
        """启动自治修复流程。

        数据流：
        - 构建 llm_caller -> 创建 AutonomousFixWorker -> 连接 Signal -> start。
        - problem 字典由调用方预处理（补充 stdin/stdout/description）。
        - all_test_cases 为全量测试用例，用于对拍验证覆盖全部数据。
        """
        llm_caller = self._create_llm_caller(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        llm_caller_factory = self._create_llm_caller_factory(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

        self._worker = AutonomousFixWorker(
            problem=problem,
            candidate_code=candidate_code,
            llm_caller=llm_caller,
            llm_caller_factory=llm_caller_factory,
            runtime_context=dict(runtime_context or {}),
            enable_cross_validation=bool(enable_cross_validation),
            all_test_cases=list(all_test_cases or []),
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.step.connect(self._on_step)
        self._worker.code_update.connect(self._on_code_update)
        self._worker.done.connect(self._on_done)
        self._worker.err.connect(self._on_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def cancel(self) -> bool:
        """协作取消 Worker，等待最多 1s。返回是否实际执行了取消。"""
        if self._worker is None or not self._worker.isRunning():
            return False
        self._worker.requestInterruption()
        self._worker.wait(1000)
        return True

    def _create_llm_caller(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
    ) -> Callable[[list[dict[str, str]]], str]:
        """创建 LLM 调用适配器，供 solve_with_agent 使用。"""
        client = OpenAICompatHTTPClient(
            base_url=base_url,
            api_key=api_key,
            timeout_sec=120,
        )

        def llm_caller(messages: list[dict[str, str]]) -> str:
            result_text = ""
            try:
                for chunk in client.stream_chat_completion(model=model, messages=messages):
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0] or {}).get("delta") or {}
                    content_piece = delta.get("content") or ""
                    if content_piece:
                        result_text += str(content_piece)
            except Exception:
                logger.exception("llm_caller 发生异常")
            return result_text

        return llm_caller

    def _create_llm_caller_factory(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
    ) -> Callable[[str], Callable[[list[dict[str, str]]], str]]:
        """创建按会话标签返回 llm_caller 的工厂函数。"""

        def factory(_session_tag: str) -> Callable[[list[dict[str, str]]], str]:
            return self._create_llm_caller(model=model, api_key=api_key, base_url=base_url)

        return factory

    def _on_progress(self, stage: str, attempt: int, max_attempts: int):
        self._status_badge.setText(f"自治修复 ({attempt}/{max_attempts}): {stage}")

    def _on_step(self, action: str, step_info: dict):
        attempt = int((step_info or {}).get("attempt") or 0)
        result_type = str((step_info or {}).get("type") or "")
        version = int((step_info or {}).get("version") or 0)

        action_text = _ACTION_NAMES.get(action, action)
        type_text = _TYPE_NAMES.get(result_type, result_type)

        log_line = f"[第{attempt}轮] {action_text}"
        if type_text:
            log_line += f" → {type_text}"
        log_line += f" (v{version})\n"

        current_text = self._left_editor.get_text()
        self._left_editor.set_text(current_text + log_line)
        self._left_editor.editor.moveCursor(QTextCursor.End)

    def _on_code_update(self, code: str):
        if code and code.strip():
            self._right_editor.set_text(code)

    def _on_done(self, result: dict):
        passed = bool((result or {}).get("passed"))
        final_code = str((result or {}).get("final_code") or "")
        attempts = int((result or {}).get("attempts") or 0)
        summary = dict((result or {}).get("summary") or {})
        cross = dict(summary.get("cross_validation") or {})
        cross_enabled = bool(cross.get("enabled"))
        cross_passed = bool(cross.get("verification_passed"))
        cross_skip_reason = str(cross.get("skip_reason") or "").strip()

        if passed:
            status_text = f"修复成功 (共 {attempts} 轮)"
            if cross_enabled:
                status_text += " + 对拍通过" if cross_passed else " + 对拍降级"
            self._status_badge.setText(status_text)
            if final_code.strip():
                self._right_editor.set_text(final_code)
            log_line = f"\n=== 修复成功，共 {attempts} 轮 ===\n"
            if cross_enabled:
                if cross_passed:
                    log_line += "=== 双算法对拍：通过 ===\n"
                else:
                    reason_text = cross_skip_reason or "未通过或已降级"
                    log_line += f"=== 双算法对拍：降级（{reason_text}） ===\n"
        else:
            reason = str(summary.get("reason") or "未知原因")
            if reason == "data_sample_manual_required":
                reason = "数据复杂，需手动提供精简样例"
            self._status_badge.setText(f"修复失败: {reason}")
            log_line = f"\n=== 修复失败: {reason} ===\n"

        current_text = self._left_editor.get_text()
        self._left_editor.set_text(current_text + log_line)
        self._left_editor.editor.moveCursor(QTextCursor.End)

    def _on_err(self, message: str):
        self._status_badge.setText("修复异常")
        self.error_occurred.emit(message)

    def _on_worker_finished(self):
        self.finished.emit()
