"""DeepSeek 代码精炼对话框。

核心职责：
- 提供可视化界面与 DeepSeek API 交互，实现代码流式精炼。
- 支持多轮对话（通过 ChatSession 维护上下文），允许用户基于前一轮结果继续追问修正。
- 双面板布局：左侧面板显示原始流式输出（含 <think> 推理块），右侧面板显示清理后的精炼代码。
- 支持模型选择、API Key 配置、评分范围注入（generate_data 任务）。
- 编排流式请求与自治修复两种模式，统一管理取消与 UI 恢复。

不负责：
- 不直接调用 HTTP API，所有网络请求通过 StreamWorker/ModelsWorker（QThread）异步执行。
- 不持久化对话历史，仅维护内存中的 _chat_session（对话框关闭即清空）。
- 不处理代码补全或语法高亮（由 CodeEditorContainer 负责，本模块仅禁用这些功能）。
- 不直接处理自治修复的 Worker 回调（由 AutonomousFixHandler 隔离，本模块仅响应 finished/error_occurred）。
- 不组装 prompt 拼接规则（由 prompt_builder 纯函数模块负责）。
- 不管理流式缓冲区与防抖刷新（由 StreamRenderHandler 隔离，本模块仅调用 on_delta/on_done/reset）。
- 不搭建界面控件（由 ui_builder.build_dialog_ui 负责，本模块仅声明属性和连接业务信号）。

主要数据流：
1. 入口：外部调用方通过 __init__ 传入 source_code/prompt/problem，或调用 queue_followup_request 追加请求。
2. 用户输入：题目描述（必填）、补充要求（可选）、评分范围（可选，仅 generate_data 任务）。
3. 请求组装：send_request 通过 prompt_builder.build_refine_prompt 构建完整 prompt，通过 ChatSession.preview_with_user 生成 messages。
4. 网络层：StreamWorker 在独立线程中调用 OpenAICompatHTTPClient.stream_chat_completion，通过 Signal 回传 delta。
5. 流式渲染：_on_stream_delta 校验 request_id 后委托 StreamRenderHandler.on_delta，Handler 通过 QTimer（120ms 防抖）批量刷新 UI。
6. 文本清理：StreamRenderHandler 内部调用 cleanup_partial_text（实时）/ cleanup_final_code（完成时）提取代码块，去除 iesa 标签。
7. 自治修复：send_request 检测模式后委托 AutonomousFixHandler.start，Handler 直接更新编辑器和状态标签。
8. 取消与恢复：_cancel_stream_worker / _cancel_autonomous_worker 收敛取消逻辑，_restore_after_cancel 统一恢复 UI。
9. 输出：用户点击"应用覆盖"，通过 get_refined_code() 获取结果，对话框 accept() 返回。

关键组件协作：
- prompt_builder：纯函数模块，负责 prompt 拼接规则（评分注入、历史追加指令等），无副作用。
- ui_builder：UI 构建模块，负责搭建全部界面控件并连接纯 UI 信号，通过 build_dialog_ui 一次性调用。
- AutonomousFixHandler：自治修复适配层，封装 Worker 创建/信号连接/回调处理，通过 finished/error_occurred 信号与 Dialog 通信。
- StreamRenderHandler：流式渲染适配层，管理缓冲区累积与防抖刷新，通过 on_delta/on_done/reset 与 Dialog 交互。
- ChatSession：维护 system + user/assistant 消息列表，提供上下文截断（max_rounds=8，单轮上限 110k 字符）。
- StreamWorker：QThread 子类，负责 SSE 流式请求，通过 delta/done/err Signal 与主线程通信。
- ModelsWorker：QThread 子类，负责获取可用模型列表。
- CodeEditorContainer：左右双编辑器容器，本模块禁用其 completer/highlighter 以提升性能。
- deepseek_config：读写系统密钥库中的 API Key（SecureSettingsStore）。

状态与数据说明：
- _chat_session：ChatSession 实例，生命周期与对话框绑定，reset() 在上下文超限时触发。
- _pending_turn_prompts：Dict[int, str]，记录每轮请求的原始 prompt，用于流式完成后 commit_turn。
- _worker：StreamWorker 实例，需检查 isRunning() 防止重复启动。
- _models_worker：ModelsWorker 实例，需检查 isRunning() 防止重复启动。
- _autonomous_handler：AutonomousFixHandler 实例，封装自治修复 Worker 的生命周期。
- _stream_handler：StreamRenderHandler 实例，封装流式缓冲区与防抖刷新。

约束：
- 题目描述长度强制 >= 20 字符，否则拒绝发送。
- 单轮请求总字符数超 110k 时自动重置会话历史，避免 API 超限错误。
- 流式过程中禁用所有输入控件（_set_busy），防止并发请求。
- 自治修复模式（mode_combo）仅显示帮助入口，实际逻辑由调用方在对话框外处理。
- 取消操作统一使用协作取消（requestInterruption），禁止 terminate。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
)

from ..common.message_box import MessageBox
from utils.markdown_renderer import render_markdown_to_html

from services.ai.deepseek_config import (
    get_deepseek_api_key,
    get_deepseek_base_url,
    get_deepseek_model,
    set_deepseek_api_key,
    set_deepseek_model,
)
from services.ai.workers import StreamWorker, ModelsWorker
from services.ai.chat_session import ChatSession
from services.ai.errors import MissingApiKeyError
from utils.ai_text_utils import clip_text
from .prompt_builder import build_refine_prompt, parse_score_range
from services.prompts.prompt_builders import (
    get_agent_system_prompt,
    get_generate_data_autonomous_guidance,
    get_generate_data_system_prompt,
    get_generate_data_system_prompt_with_scoring,
    get_solve_system_prompt,
)
from .autonomous_fix_handler import AutonomousFixHandler
from .stream_render_handler import StreamRenderHandler
from .ui_builder import build_dialog_ui
from ...styles.deepseek_refine_dialog_style import DEEPSEEK_REFINE_DIALOG_QSS
from .api_key_dialog import ApiKeyDialog

logger = logging.getLogger(__name__)


class DeepSeekRefineDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        title: str = "DeepSeek 精炼",
        task_name: str = "generate_data",
        source_code: str = "",
        prompt: str = "",
        problem: Optional[Dict[str, Any]] = None,
        all_test_cases: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(1180, 720)
        self.setStyleSheet(DEEPSEEK_REFINE_DIALOG_QSS)

        self._task_name = (task_name or "").strip() or "generate_data"
        self._problem = dict(problem or {})
        self._prompt = str(prompt or "").strip()
        self._all_test_cases: List[Dict[str, Any]] = list(all_test_cases or [])
        self._worker: Optional[StreamWorker] = None
        self._models_worker: Optional[ModelsWorker] = None
        self._autonomous_handler: Optional[AutonomousFixHandler] = None
        self._request_seq = 0
        self._active_request_id = 0
        self._source_code = source_code or ""
        # ChatSession 延迟到 send_request 中创建，以便根据评分范围动态选择 system prompt
        self._chat_session: Optional[ChatSession] = None
        self._pending_turn_prompts: Dict[int, str] = {}

        build_dialog_ui(self)

        self._stream_handler = StreamRenderHandler(
            left_editor=self.left_editor,
            right_editor=self.right_editor,
            parent=self,
        )

        self._autonomous_handler = AutonomousFixHandler(
            left_editor=self.left_editor,
            right_editor=self.right_editor,
            status_badge=self.status_badge,
            parent=self,
        )
        self._autonomous_handler.finished.connect(self._on_autonomous_handler_finished)
        self._autonomous_handler.error_occurred.connect(self._on_autonomous_handler_error)

    def get_refined_code(self) -> str:
        return self.right_editor.get_text()

    def _resolve_system_prompt(self, task_name: str, has_scoring: bool = False) -> str:
        """根据任务类型返回对应的 system prompt（角色定义）。

        规则：
        - solve -> 解题专家角色
        - generate_data -> 数据生成专家角色（带评分规则如果 has_scoring=True）
        - 其他 -> 通用助手（兜底）
        """
        tn = str(task_name or "").strip().lower()
        if tn == "solve":
            return get_solve_system_prompt()
        elif tn == "generate_data":
            if has_scoring:
                return get_generate_data_system_prompt_with_scoring()
            return get_generate_data_system_prompt()
        return "You are a helpful assistant."

    def queue_followup_request(self, user_problem: str, user_extra: str = "") -> None:
        """外部调用入口，用于追加问题并自动触发请求。

        调用来源：外部组件（如自治修复循环）需要基于前一轮结果继续追问时调用。
        行为：自动填充题目输入框和补充要求，然后通过 QTimer 异步触发 send_request。
        约束：空 problem_text 直接返回，不做任何操作。
        """
        problem_text = str(user_problem or "").strip()
        if not problem_text:
            return
        self.problem_input.setPlainText(clip_text(problem_text, 28_000))
        self.extra_input.setPlainText(clip_text(str(user_extra or "").strip(), 8_000))
        QTimer.singleShot(0, self.send_request)

    def _toggle_splitter_orientation(self):
        if self.splitter.orientation() == Qt.Horizontal:
            self.splitter.setOrientation(Qt.Vertical)
            self.btn_toggle_layout.setText("切换左右")
        else:
            self.splitter.setOrientation(Qt.Horizontal)
            self.btn_toggle_layout.setText("切换上下")

    def _on_mode_changed(self, mode: str):
        is_autonomous = str(mode or "").strip() == "自治修复"
        self.btn_mode_help.setVisible(is_autonomous)
        self.cross_validation_checkbox.setVisible(is_autonomous)

    def _show_mode_help(self):
        help_file = Path(__file__).parent.parent.parent.parent / "File" / "API_Help_text.txt"
        help_text = help_file.read_text(encoding="utf-8")
        html_text = render_markdown_to_html(help_text)
        MessageBox.information(self, "自治修复说明", html_text)

    def _configure_key(self):
        dlg = ApiKeyDialog(self)
        if dlg.exec():
            set_deepseek_api_key(dlg.get_api_key())

    def _ensure_key(self) -> bool:
        if get_deepseek_api_key():
            return True
        dlg = ApiKeyDialog(self)
        if not dlg.exec():
            return False
        set_deepseek_api_key(dlg.get_api_key())
        return True

    def _set_busy(self, busy: bool):
        """切换 UI 忙闲状态。

        约束：流式过程中禁用所有操作按钮和输入框，防止并发请求或数据竞争。
        """
        self.btn_send.setEnabled(not busy)
        self.btn_send.setVisible(not busy)
        self.btn_cancel.setEnabled(busy)
        self.btn_cancel.setVisible(busy)
        self.btn_config_key.setEnabled(not busy)
        self.btn_toggle_layout.setEnabled(not busy)
        self.btn_refresh_models.setEnabled(not busy)
        self.model_combo.setEnabled(not busy)
        self.mode_combo.setEnabled(not busy)
        self.btn_mode_help.setEnabled(not busy)
        self.cross_validation_checkbox.setEnabled(not busy)
        self.btn_apply.setEnabled(not busy)
        self.problem_input.setReadOnly(bool(busy))
        self.extra_input.setReadOnly(bool(busy))
        self.score_min_input.setReadOnly(bool(busy))
        self.score_max_input.setReadOnly(bool(busy))

    def _on_model_changed(self, model: str):
        model = str(model or "").strip()
        if model:
            set_deepseek_model(model)

    def refresh_models(self, silent: bool = False):
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        if not get_deepseek_api_key():
            if silent:
                return
            if not self._ensure_key():
                return
        self.status_badge.setText("加载模型…")
        self._models_worker = ModelsWorker(self)
        self._models_worker.ok.connect(self._on_models_ok)
        self._models_worker.err.connect(lambda m: self._on_models_err(m, silent=silent))
        self._models_worker.finished.connect(lambda: self.status_badge.setText("就绪"))
        self._models_worker.start()

    def _on_models_ok(self, models: list):
        current = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            for m in models or []:
                self.model_combo.addItem(str(m))
            if current:
                idx = self.model_combo.findText(current)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
        finally:
            self.model_combo.blockSignals(False)

        if not self.model_combo.currentText():
            fallback = get_deepseek_model()
            if fallback:
                self.model_combo.addItem(fallback)
                self.model_combo.setCurrentText(fallback)

    def _on_models_err(self, message: str, *, silent: bool):
        if silent:
            return
        MessageBox.information(self, "提示", f"获取模型列表失败：{message}")

    def send_request(self):
        """组装 prompt 并启动流式请求。

        边界与约束：
        - 题目描述强制 >= 20 字符，否则拒绝。
        - 首轮使用外部传入的 _prompt，后续轮次追加"继续修正"指令。
        - generate_data 任务支持评分范围注入，异常输入静默忽略。
        - 上下文超限时（>110k 字符）自动 reset 会话并截断 prompt。
        - 通过 _pending_turn_prompts 记录本轮 prompt，供 _on_stream_done 提交到历史。
        - 生题模式下自治修复降级为普通请求。
        """
        if self._task_name == "generate_data" and self.mode_combo.currentText() == "自治修复":
            self.mode_combo.setCurrentText("普通请求")

        if not self._prompt:
            MessageBox.information(self, "提示", "缺少 prompt，无法生成。")
            return
        if not self._ensure_key():
            return

        if self._worker is not None and self._worker.isRunning():
            return
        if self._autonomous_handler is not None and self._autonomous_handler.is_running():
            return

        user_problem = (self.problem_input.toPlainText() or "").strip()
        if len(user_problem) <= 20:
            MessageBox.information(self, "提示", "题目描述为必填，且长度需要大于 20。")
            return
        user_problem = clip_text(user_problem, 32_000)

        if self.mode_combo.currentText() == "自治修复":
            self._start_autonomous_fix(user_problem)
            return

        self._set_busy(True)
        self.status_badge.setText("请求中…")
        self.btn_send.setText("请求中…")
        self._request_seq += 1
        self._active_request_id = self._request_seq
        self._stream_handler.reset()
        self.left_editor.set_text("")
        self.right_editor.set_text("")

        user_extra = (self.extra_input.toPlainText() or "").strip()
        user_extra = clip_text(user_extra, 10_000)
        score_min_text = (self.score_min_input.text() or "").strip()
        score_max_text = (self.score_max_input.text() or "").strip()
        score_range = parse_score_range(score_min_text, score_max_text)
        has_scoring = score_range is not None

        # 动态创建/重建 ChatSession（首轮或评分设置变化时）
        if self._chat_session is None or not self._chat_session.has_history():
            system_prompt = self._resolve_system_prompt(self._task_name, has_scoring=has_scoring)
            self._chat_session = ChatSession(system_prompt=system_prompt, max_rounds=8)

        prompt = build_refine_prompt(
            base_prompt=self._prompt,
            user_problem=user_problem,
            user_extra=user_extra,
            score_range=None,  # 评分规则已在 system prompt 中，不再传入 user prompt
            has_history=self._chat_session.has_history(),
            task_name=self._task_name,
        )

        api_key = get_deepseek_api_key()
        if not api_key:
            self._set_busy(False)
            self.status_badge.setText("就绪")
            self.btn_send.setText("发送请求")
            MessageBox.information(self, "提示", "未配置 DeepSeek API Key。")
            return

        model = str(self.model_combo.currentText() or get_deepseek_model()).strip() or get_deepseek_model()
        base_url = get_deepseek_base_url()
        messages = self._chat_session.preview_with_user(prompt)
        total_chars = sum(len(str((m or {}).get("content") or "")) for m in messages)
        if total_chars > 110_000:
            self._chat_session.reset()
            prompt = clip_text(prompt, 60_000)
            messages = self._chat_session.preview_with_user(prompt)
        self._pending_turn_prompts[self._active_request_id] = prompt

        self._worker = StreamWorker(
            request_id=self._active_request_id,
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            parent=self,
        )
        self._worker.delta.connect(self._on_stream_delta)
        self._worker.done.connect(self._on_stream_done)
        self._worker.err.connect(self._on_worker_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self):
        self._stream_handler.stop_flush_timer()
        self._set_busy(False)
        self.status_badge.setText("就绪")
        self.btn_send.setText("发送请求")

    def _start_autonomous_fix(self, user_problem: str):
        """启动自治修复流程，委托给 AutonomousFixHandler。"""
        candidate_code = self._source_code or ""
        if not candidate_code.strip():
            candidate_code = self.right_editor.get_text() or ""

        self._set_busy(True)
        self.status_badge.setText("自治修复中…")
        self.btn_send.setText("修复中…")
        self._stream_handler.stop_flush_timer()
        self.left_editor.set_text("")
        self.right_editor.set_text("")

        problem = dict(self._problem or {})
        if "stdin" not in problem and "stdout" not in problem:
            problem["stdin"] = ""
            problem["stdout"] = ""
        if user_problem:
            problem["description"] = str(user_problem)
        runtime_context = self._build_autonomous_runtime_context(problem)

        model = str(self.model_combo.currentText() or get_deepseek_model()).strip() or get_deepseek_model()
        api_key = get_deepseek_api_key()
        base_url = get_deepseek_base_url()

        self._autonomous_handler.start(
            problem=problem,
            candidate_code=candidate_code,
            model=model,
            api_key=api_key,
            base_url=base_url,
            runtime_context=runtime_context,
            enable_cross_validation=bool(self.cross_validation_checkbox.isChecked()),
            all_test_cases=list(self._all_test_cases or []),
        )

    def _build_autonomous_runtime_context(self, problem: Dict[str, Any]) -> Dict[str, Any]:
        samples = self._collect_data_samples_for_autonomous(problem)
        system_prompt = get_agent_system_prompt()
        if self._task_name == "generate_data":
            system_prompt += get_generate_data_autonomous_guidance()
        return {
            "problem": dict(problem or {}),
            "data_samples": samples,
            "agent_system_prompt": system_prompt,
        }

    def _collect_data_samples_for_autonomous(self, problem: Dict[str, Any]) -> List[Dict[str, Any]]:
        samples: List[Dict[str, Any]] = []
        obj = dict(problem or {})

        for key in ("data_samples", "samples", "cases"):
            value = obj.get(key)
            if isinstance(value, list):
                for item in value:
                    normalized = self._normalize_sample_item(item)
                    if normalized is not None:
                        samples.append(normalized)

        if any(k in obj for k in ("stdin", "stdout", "vars", "meta")):
            normalized_problem = self._normalize_sample_item(obj)
            if normalized_problem is not None:
                samples.append(normalized_problem)

        parent = self.parent()
        if parent is not None and hasattr(parent, "get_last_generated_samples"):
            try:
                last_generated = parent.get_last_generated_samples()
                if isinstance(last_generated, list):
                    for item in last_generated:
                        normalized = self._normalize_sample_item(item)
                        if normalized is not None:
                            samples.append(normalized)
            except Exception:
                logger.exception("读取最近生成样本失败")

        dedup: Dict[str, Dict[str, Any]] = {}
        for item in samples:
            key = str(item)
            if key not in dedup:
                dedup[key] = item
        return list(dedup.values())

    def _normalize_sample_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if isinstance(item, dict):
            return {
                "stdin": str(item.get("stdin") or ""),
                "stdout": str(item.get("stdout") or ""),
                "vars": dict(item.get("vars") or {}) if isinstance(item.get("vars"), dict) else {},
                "meta": dict(item.get("meta") or {}) if isinstance(item.get("meta"), dict) else {},
            }
        if isinstance(item, str):
            return {"stdin": item, "stdout": "", "vars": {}, "meta": {}}
        return None

    def _on_autonomous_handler_finished(self):
        """Handler 的 Worker 线程结束，恢复 UI。"""
        self._set_busy(False)
        self.btn_send.setText("发送请求")

    def _on_autonomous_handler_error(self, message: str):
        """Handler 报告错误，弹窗提示。"""
        MessageBox.information(self, "提示", f"自治修复失败：{message}")

    def _cancel_stream_worker(self):
        """协作取消流式 Worker，递增请求序号使旧请求回调失效。"""
        if self._worker is None or not self._worker.isRunning():
            return False
        self._worker.requestInterruption()
        self._request_seq += 1
        self._active_request_id = self._request_seq
        self._pending_turn_prompts.clear()
        self._stream_handler.stop_flush_timer()
        return True

    def _cancel_autonomous_worker(self):
        """协作取消自治修复 Handler，等待最多 1s 让线程退出。"""
        if self._autonomous_handler is None:
            return False
        return self._autonomous_handler.cancel()

    def _restore_after_cancel(self):
        """取消后恢复 UI 到就绪状态。"""
        self._set_busy(False)
        self.status_badge.setText("已取消")
        self.btn_send.setText("发送请求")

    def _cancel_current_request(self):
        """取消当前请求（流式或自治修复）。"""
        cancelled_stream = self._cancel_stream_worker()
        cancelled_autonomous = self._cancel_autonomous_worker()
        if cancelled_stream or cancelled_autonomous:
            self._restore_after_cancel()

    def _on_stream_delta(self, request_id: int, reasoning_piece: str, content_piece: str):
        """处理流式增量数据，委托 StreamRenderHandler。

        注意：非当前活跃请求（_active_request_id）的数据被丢弃，防止旧请求干扰。
        """
        if int(request_id) != int(self._active_request_id):
            return
        self._stream_handler.on_delta(reasoning_piece, content_piece)

    def _on_worker_err(self, request_id: int, message: str):
        """处理流式请求错误。

        特殊处理：
        - 上下文超限错误：自动 reset _chat_session，清空历史后提示用户精简输入。
        - API Key 缺失：抛出 MissingApiKeyError 并提示配置。
        - 其他错误：直接弹窗显示原始错误信息。

        副作用：从 _pending_turn_prompts 移除对应请求，防止内存泄漏。
        """
        if int(request_id) != int(self._active_request_id):
            return
        self._pending_turn_prompts.pop(int(request_id), None)
        lower_msg = str(message or "").lower()
        if "maximum context length" in lower_msg or "invalid_request_error" in lower_msg:
            self._chat_session.reset()
            MessageBox.information(self, "提示", "本次请求上下文过长，已自动清空会话历史。请精简测试数据后重试。")
            return
        if "deepseek_api_key_missing" in (message or ""):
            try:
                raise MissingApiKeyError(message)
            except MissingApiKeyError:
                MessageBox.information(self, "提示", "未配置 DeepSeek API Key。")
                return
        MessageBox.information(self, "提示", f"生成失败：{message}")

    def _on_stream_done(self, request_id: int, reasoning_text: str, content_text: str):
        """流式完成回调，委托 Handler 刷新 UI 并提交对话历史。

        注意：非当前活跃请求的数据被丢弃。
        """
        if int(request_id) != int(self._active_request_id):
            return
        turn_prompt = self._pending_turn_prompts.pop(int(request_id), "")
        cleaned = self._stream_handler.on_done(reasoning_text, content_text)
        self._chat_session.commit_turn(user_content=turn_prompt, assistant_content=str(content_text or ""))

    def reject(self):
        self._cancel_stream_worker()
        self._cancel_autonomous_worker()
        self._restore_after_cancel()
        super().reject()

    def _on_apply(self):
        if not self.get_refined_code().strip():
            MessageBox.information(self, "提示", "精炼结果为空，无法应用覆盖。")
            return
        self.accept()
