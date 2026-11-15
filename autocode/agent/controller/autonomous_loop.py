"""autoCode Agent 自治循环控制器。

核心职责：
- 驱动 “模型决策 -> 动作分发 -> 结果回填” 的闭环迭代。
- 维护 OpenAI 兼容消息窗口，固定保留 system / 题目 / 最新代码三条消息。
- 内置防死循环控制（最大尝试次数、协议错误阈值、无进展阈值、重复动作阈值）。

不负责：
- 不实现代码执行细节（由 runtime executor 注入 dispatcher）。
- 不管理多会话池；调用方持有并传入单个会话状态对象。

数据流：
- `problem + state.current_code` -> 组装固定消息 ->
  LLM 输出动作 JSON -> dispatcher 执行动作 ->
  结果摘要写回动态消息窗口 -> 下一轮。

约束：
- 默认最多尝试 30 次，可通过配置覆盖。
- 对外只返回最终摘要与 final_code，不暴露隐藏测试细节。

P0 重构说明（R4 函数 ≤150 行）：
- 原 `run_autonomous_loop` 单函数 257 行，超 R4 上限。此处拆为
  `_AutonomousRun`（运行态 + 单轮动作 + 防打转记账）与主循环，
  **判定顺序、trace 事件、返回值结构逐条保持不变**，由
  `tests/test_loop_behavior.py` 做行为回归。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Callable, Dict, List, Optional

from ..contracts.protocol import (
    ACTION_FINALIZE,
    ACTION_MODIFY,
    ACTION_READ,
    ACTION_RUN,
    ACTION_SELF_TEST,
    CODE_CONTEXT_MAX_CHARS,
    DEFAULT_CONTEXT_ROUNDS,
    DEFAULT_MAX_AUTONOMOUS_ATTEMPTS,
    MAX_CONTEXT_ROUNDS,
    MAX_SAME_ACTION_ROUNDS,
    PROBLEM_CONTEXT_MAX_CHARS,
    RESULT_FINALIZE_OK,
    RESULT_MODIFY_OK,
    RESULT_PROTOCOL_ERROR,
)
from ..session.state import AgentSessionState
from ..trace import append_agent_trace_code_version, append_agent_trace_message
from .dispatcher import ActionDispatcher, RunExecutor
from .messages import AutonomousMessageManager

LlmCaller = Callable[[List[Dict[str, str]]], Any]


@dataclass
class LoopConfig:
    """自治循环配置。

    `max_attempts` 为硬上限，防止智能体在 wrong/runtime_error 场景下无限循环。
    `on_step_callback` 为可选回调，每完成一个动作后触发，用于 UI 进度通知。
    """

    max_attempts: int = DEFAULT_MAX_AUTONOMOUS_ATTEMPTS
    max_protocol_errors: int = 3
    max_no_progress_rounds: int = 5
    context_rounds: int = DEFAULT_CONTEXT_ROUNDS
    problem_message_max_chars: int = PROBLEM_CONTEXT_MAX_CHARS
    code_message_max_chars: int = CODE_CONTEXT_MAX_CHARS
    # 兼容旧配置项：历史上这里是“题目卡”的最大长度，现已切换为固定题目消息。
    problem_card_max_chars: int = PROBLEM_CONTEXT_MAX_CHARS
    on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    should_stop: Optional[Callable[[], bool]] = None


@dataclass
class _AttemptOutcome:
    """单轮尝试的结构化结果（供主循环做停止判定）。

    `step_info` 已携带完整 action_payload 与 result，故此处只留判定所需的最小字段。
    """

    result_type: str = ""
    protocol_error_code: str = ""
    action_name: str = ""
    before_hash: str = ""
    after_hash: str = ""
    step_info: Dict[str, Any] = field(default_factory=dict)
    short_step: Dict[str, Any] = field(default_factory=dict)
    parse_failed: bool = False
    stop_after_llm: bool = False


def run_autonomous_loop(
    *,
    state: AgentSessionState,
    problem: Dict[str, Any],
    llm_caller: LlmCaller,
    dispatcher: ActionDispatcher,
    config: Optional[LoopConfig] = None,
) -> Dict[str, Any]:
    cfg = config or LoopConfig()
    run = _AutonomousRun(
        state=state,
        problem=problem,
        llm_caller=llm_caller,
        dispatcher=dispatcher,
        config=cfg,
    )
    run.emit_start_trace()

    for attempt in range(1, run.max_attempts + 1):
        if run.should_stop(attempt=attempt, reason="interruption_requested"):
            break

        outcome = run.run_attempt(attempt)
        if outcome.stop_after_llm:
            break
        if outcome.parse_failed:
            if run.protocol_errors >= run.max_protocol_errors:
                break
            continue

        run.steps.append(outcome.short_step)
        if run.should_stop(attempt=attempt, reason="interruption_requested_after_dispatch"):
            break
        run.emit_step_callback(outcome.step_info)
        if outcome.result_type == RESULT_FINALIZE_OK:
            return run.build_success(attempt=attempt, result_type=outcome.result_type)
        if run.count_protocol_error(attempt=attempt, outcome=outcome):
            break
        if run.note_action_progress(attempt=attempt, outcome=outcome):
            break

    return run.build_failure()


class _AutonomousRun:
    """单次自治循环运行态。

    设计要点：
    - 每次 `run_autonomous_loop` 新建一个实例，计数器/消息窗口不跨 run 共享，
      避免并行会话互相污染。
    - 停止判定拆成独立方法，保证主循环只负责“顺序调用 + 何时退出”。
    """

    def __init__(
        self,
        *,
        state: AgentSessionState,
        problem: Dict[str, Any],
        llm_caller: LlmCaller,
        dispatcher: ActionDispatcher,
        config: LoopConfig,
    ) -> None:
        self.state = state
        self.llm_caller = llm_caller
        self.dispatcher = dispatcher
        self.config = config
        self.max_attempts = max(1, int(config.max_attempts or DEFAULT_MAX_AUTONOMOUS_ATTEMPTS))
        self.max_protocol_errors = max(1, int(config.max_protocol_errors or 3))
        self.max_no_progress_rounds = max(1, int(config.max_no_progress_rounds or 5))
        self.context_rounds = min(
            MAX_CONTEXT_ROUNDS,
            max(1, int(config.context_rounds or DEFAULT_CONTEXT_ROUNDS)),
        )
        self.problem_max_chars = max(
            512,
            int(config.problem_message_max_chars or config.problem_card_max_chars or PROBLEM_CONTEXT_MAX_CHARS),
        )
        self.code_max_chars = max(512, int(config.code_message_max_chars or CODE_CONTEXT_MAX_CHARS))

        self.protocol_errors = 0
        self.no_progress_rounds = 0
        self.repeated_action_name = ""
        self.repeated_action_rounds = 0
        self.stop_reason_override = ""
        self.steps: List[Dict[str, Any]] = []
        self.message_manager = AutonomousMessageManager(
            system_prompt=_resolve_system_prompt(state),
            problem=dict(problem or {}),
            code_text=state.current_code,
            version=state.version,
            code_hash=state.code_hash,
            max_rounds=self.context_rounds,
            problem_max_chars=self.problem_max_chars,
            code_max_chars=self.code_max_chars,
        )

    # ── trace / 停止判定 ────────────────────────────────────────────────────

    def emit_start_trace(self) -> None:
        _append_trace(
            self.state,
            "loop_start",
            {
                "max_attempts": self.max_attempts,
                "max_protocol_errors": self.max_protocol_errors,
                "max_no_progress_rounds": self.max_no_progress_rounds,
                "max_same_action_rounds": int(MAX_SAME_ACTION_ROUNDS),
                "context_rounds": self.context_rounds,
                "problem_message_max_chars": self.problem_max_chars,
                "code_message_max_chars": self.code_max_chars,
            },
        )

    def should_stop(self, *, attempt: int, reason: str) -> bool:
        if not _should_stop(self.config):
            return False
        _append_trace(self.state, "loop_stopped", {"attempt": attempt, "reason": reason})
        return True

    def emit_step_callback(self, step_info: Dict[str, Any]) -> None:
        """触发 UI 进度回调；回调异常不得影响主流程。"""
        if not self.config.on_step_callback:
            return
        try:
            self.config.on_step_callback(step_info)
        except Exception:
            pass

    # ── 单轮 ────────────────────────────────────────────────────────────────

    def run_attempt(self, attempt: int) -> _AttemptOutcome:
        state = self.state
        messages = self.message_manager.build_messages()
        llm_output = self.llm_caller(messages)
        if self.should_stop(attempt=attempt, reason="interruption_requested_after_llm"):
            return _AttemptOutcome(stop_after_llm=True)

        action_payload = _normalize_llm_action(llm_output)
        if action_payload:
            _append_trace(
                state,
                "action_payload",
                {"attempt": attempt, "payload": dict(action_payload or {})},
            )
        if not action_payload:
            return self._record_unparsable_output(attempt=attempt, llm_output=llm_output)

        before_version = int(state.version)
        before_hash = str(state.code_hash)
        before_code = str(state.current_code or "")
        result = self.dispatcher.dispatch(action_payload, state)
        result_type = str((result or {}).get("type") or "")
        protocol_error_code = str((result or {}).get("error_code") or "")
        _append_trace(
            state,
            "dispatch_result",
            {
                "attempt": attempt,
                "action": str(action_payload.get("action") or ""),
                "result_type": result_type,
            },
        )
        self.message_manager.append_action_summary(attempt=attempt, action_payload=action_payload)
        if (
            int(state.version) != before_version
            or str(state.code_hash or "") != before_hash
            or str(state.current_code or "") != before_code
        ):
            self.message_manager.update_code(
                code_text=state.current_code,
                version=state.version,
                code_hash=state.code_hash,
            )
        self.message_manager.append_result_summary(
            attempt=attempt,
            action_payload=action_payload,
            result=dict(result or {}),
        )
        action_name = str(action_payload.get("action") or "")
        return _AttemptOutcome(
            result_type=result_type,
            protocol_error_code=protocol_error_code,
            action_name=action_name,
            before_hash=before_hash,
            after_hash=str(state.code_hash),
            step_info={
                "attempt": attempt,
                "max_attempts": self.max_attempts,
                "action": action_name,
                "action_payload": dict(action_payload or {}),
                "type": result_type,
                "version": int(state.version),
                "code": state.current_code,
                "result": dict(result or {}),  # 完整运行结果，供 UI 展示 stdout/stderr 等
            },
            short_step={
                "attempt": attempt,
                "action": action_name,
                "type": result_type,
                "version": int(state.version),
            },
        )

    def _record_unparsable_output(self, *, attempt: int, llm_output: Any) -> _AttemptOutcome:
        self.message_manager.append_invalid_output(attempt=attempt, raw_output=llm_output)
        self.protocol_errors += 1
        self.state.push_event(
            action="invalid",
            result_type=RESULT_PROTOCOL_ERROR,
            note="llm_action_parse_failed",
        )
        _append_trace(
            self.state,
            "protocol_error",
            {
                "attempt": attempt,
                "reason": "llm_action_parse_failed",
                "protocol_errors": self.protocol_errors,
            },
        )
        self.steps.append(
            {
                "attempt": attempt,
                "action": "invalid",
                "type": RESULT_PROTOCOL_ERROR,
                "note": "llm_action_parse_failed",
            }
        )
        return _AttemptOutcome(parse_failed=True)

    # ── 停止条件记账 ────────────────────────────────────────────────────────

    def count_protocol_error(self, *, attempt: int, outcome: _AttemptOutcome) -> bool:
        """协议错误计数；返回 True 表示需要终止循环。"""
        if outcome.result_type != RESULT_PROTOCOL_ERROR:
            self.protocol_errors = 0
            return False
        if outcome.protocol_error_code == "data_sample_too_complex":
            self.stop_reason_override = "data_sample_manual_required"
            _append_trace(
                self.state,
                "loop_stop_reason",
                {
                    "attempt": attempt,
                    "reason": self.stop_reason_override,
                    "protocol_error_code": outcome.protocol_error_code,
                },
            )
            return True
        self.protocol_errors += 1
        return self.protocol_errors >= self.max_protocol_errors

    def note_action_progress(self, *, attempt: int, outcome: _AttemptOutcome) -> bool:
        """重复动作 / 无进展记账；返回 True 表示需要终止循环。"""
        action_name = outcome.action_name
        if action_name == ACTION_READ:
            self.repeated_action_name = ""
            self.repeated_action_rounds = 0
        elif action_name:
            if action_name == self.repeated_action_name:
                self.repeated_action_rounds += 1
            else:
                self.repeated_action_name = action_name
                self.repeated_action_rounds = 1
            if self.repeated_action_rounds >= int(MAX_SAME_ACTION_ROUNDS):
                self.stop_reason_override = "same_action_limit_reached"
                _append_trace(
                    self.state,
                    "loop_stop_reason",
                    {
                        "attempt": attempt,
                        "reason": self.stop_reason_override,
                        "action": action_name,
                        "same_action_rounds": self.repeated_action_rounds,
                    },
                )
                return True

        if action_name == ACTION_MODIFY and outcome.before_hash != outcome.after_hash:
            self.no_progress_rounds = 0
        elif action_name in {ACTION_RUN, ACTION_MODIFY, ACTION_SELF_TEST}:
            self.no_progress_rounds += 1

        if self.no_progress_rounds >= self.max_no_progress_rounds:
            _append_trace(
                self.state,
                "loop_stop_reason",
                {
                    "attempt": attempt,
                    "reason": "no_progress_limit_reached",
                    "no_progress_rounds": self.no_progress_rounds,
                },
            )
            return True

        if action_name == ACTION_FINALIZE and outcome.result_type != RESULT_FINALIZE_OK:
            self.no_progress_rounds += 1
        return False

    # ── 结果构造 ────────────────────────────────────────────────────────────

    def build_success(self, *, attempt: int, result_type: str) -> Dict[str, Any]:
        _append_trace(self.state, "loop_success", {"attempt": attempt, "reason": "finalize_ok"})
        return {
            "passed": True,
            "session_id": self.state.session_id,
            "attempts": attempt,
            "final_code": self.state.current_code,
            "summary": {
                "status": "ok",
                "type": result_type,
                "recent_events": list(self.state.recent_events),
                "steps": self.steps,
            },
        }

    def build_failure(self) -> Dict[str, Any]:
        _append_trace(
            self.state,
            "loop_failed",
            {
                "attempts": min(self.max_attempts, len(self.steps)),
                "protocol_errors": self.protocol_errors,
                "no_progress_rounds": self.no_progress_rounds,
            },
        )
        return {
            "passed": False,
            "session_id": self.state.session_id,
            "attempts": min(self.max_attempts, len(self.steps)),
            "final_code": self.state.current_code,
            "summary": {
                "status": "failed",
                "reason": _build_stop_reason(
                    stop_reason_override=self.stop_reason_override,
                    protocol_errors=self.protocol_errors,
                    no_progress_rounds=self.no_progress_rounds,
                    max_protocol_errors=self.max_protocol_errors,
                    max_no_progress_rounds=self.max_no_progress_rounds,
                ),
                "recent_events": list(self.state.recent_events),
                "steps": self.steps,
            },
        }


def create_autonomous_dispatcher(*, run_executor: Optional[RunExecutor] = None) -> ActionDispatcher:
    return ActionDispatcher(run_executor=run_executor)


def _resolve_system_prompt(state: AgentSessionState) -> str:
    system_prompt = str(state.runtime_context.get("agent_system_prompt") or "").strip()
    if not system_prompt:
        system_prompt = (
            "你是隔离代码修复 Agent。\n"
            "你每一轮必须且只能输出一个 JSON 对象，不得输出 markdown、代码块、注释、解释文本。\n"
            "可用 action 只有：read/run/modify/self_test/finalize。\n"
            "严格协议：\n"
            "1) read: {\"action\":\"read\",\"target\":\"code|problem|all|data_sample\"}\n"
            "2) run（推荐）: {\"action\":\"run\",\"use_current\":true} 直接运行当前会话最新代码。\n"
            "   run（显式）: {\"action\":\"run\",\"code\":\"<完整代码字符串>\"} 两者互斥，优先用 use_current。\n"
            "3) modify: {\"action\":\"modify\",\"code\":\"<完整代码字符串>\"}\n"
            "4) self_test: {\"action\":\"self_test\",\"cases\":[{\"stdin\":\"...\",\"timeout_sec\":2.0}]}\n"
            "5) finalize: {\"action\":\"finalize\",\"code_hash\":\"可选\"}\n"
            "关键约束：modify 没有 code 字段会被判定 protocol_error(missing_field)；run 必须有 use_current=true 或 code 字段之一。\n"
            "如果题目复杂可按需调用 read(target=data_sample) 获取当前最短测试数据源结构。\n"
            "如果 read(target=data_sample) 返回 data_sample_too_complex，表示样本过大，需要人工提供精简样例后再继续。"
        )
    return system_prompt


def _normalize_llm_action(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right > left:
        chunk = text[left : right + 1]
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _build_stop_reason(
    *,
    stop_reason_override: str,
    protocol_errors: int,
    no_progress_rounds: int,
    max_protocol_errors: int,
    max_no_progress_rounds: int,
) -> str:
    if str(stop_reason_override or "").strip():
        return str(stop_reason_override)
    if protocol_errors >= max_protocol_errors:
        return "protocol_error_limit_reached"
    if no_progress_rounds >= max_no_progress_rounds:
        return "no_progress_limit_reached"
    return "max_attempts_reached"


def _should_stop(cfg: LoopConfig) -> bool:
    if not cfg.should_stop:
        return False
    try:
        return bool(cfg.should_stop())
    except Exception:
        return False


def _append_trace(state: AgentSessionState, event: str, payload: Dict[str, Any]) -> None:
    try:
        run_id = str((state.runtime_context or {}).get("trace_run_id") or "").strip()
        if not run_id:
            return
        message_item = _build_trace_message_item(state=state, event=event, payload=payload)
        if message_item:
            append_agent_trace_message(run_id, message_item)
        if (
            str(event or "") == "dispatch_result"
            and str((payload or {}).get("action") or "") == ACTION_MODIFY
            and str((payload or {}).get("result_type") or "") == RESULT_MODIFY_OK
        ):
            append_agent_trace_code_version(
                run_id,
                slot="single_agent",
                phase="modify",
                code=str(state.current_code or ""),
                success=True,
                rounds=int((payload or {}).get("attempt") or 0),
                extra={
                    "version": int(state.version),
                    "code_hash": str(state.code_hash or ""),
                },
            )
    except Exception:
        return


def _build_trace_message_item(
    *,
    state: AgentSessionState,
    event: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    evt = str(event or "")
    data = dict(payload or {})
    stage = evt
    message = ""
    details: Dict[str, Any] = {
        "session_id": str(state.session_id or ""),
        "version": int(state.version),
        "code_hash": str(state.code_hash or ""),
    }
    if evt == "loop_start":
        message = "开始自治修复"
        details.update(
            {
                "max_attempts": int(data.get("max_attempts") or 0),
                "context_rounds": int(data.get("context_rounds") or 0),
            }
        )
    elif evt == "action_payload":
        attempt = int(data.get("attempt") or 0)
        payload = _compact_action_payload(data.get("payload"))
        action = str(payload.get("action") or "")
        message = f"第{attempt}轮 动作决策: {action or 'unknown'}"
        details.update({"attempt": attempt, "payload": payload})
    elif evt == "dispatch_result":
        attempt = int(data.get("attempt") or 0)
        action = str(data.get("action") or "")
        result_type = str(data.get("result_type") or "")
        message = f"第{attempt}轮 {action} -> {result_type}"
        details.update(
            {
                "attempt": attempt,
                "action": action,
                "result_type": result_type,
            }
        )
    elif evt == "protocol_error":
        attempt = int(data.get("attempt") or 0)
        reason = str(data.get("reason") or "")
        message = f"第{attempt}轮 协议错误: {reason}"
        details.update({"attempt": attempt, "reason": reason})
    elif evt == "loop_stop_reason":
        reason = str(data.get("reason") or "")
        message = f"停止原因: {reason}"
        details.update({"attempt": int(data.get("attempt") or 0), "reason": reason})
    elif evt == "loop_stopped":
        message = "自治修复被中断"
        details.update({"reason": str(data.get("reason") or "")})
    elif evt == "loop_success":
        message = "自治修复完成"
        details.update({"attempt": int(data.get("attempt") or 0), "reason": str(data.get("reason") or "")})
    elif evt == "loop_failed":
        message = "自治修复结束，未通过"
        details.update(
            {
                "attempts": int(data.get("attempts") or 0),
                "protocol_errors": int(data.get("protocol_errors") or 0),
                "no_progress_rounds": int(data.get("no_progress_rounds") or 0),
            }
        )
    else:
        return {}

    return {
        "stage": stage,
        "message": message,
        "mode": "single_agent",
        "details": details,
    }


def _compact_action_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    compact: Dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key or "")
        if name == "code":
            code_text = str(value or "")
            compact["code"] = {
                "stored_in_code_versions": True,
                "hash": _sha256_text(code_text),
                "chars": len(code_text),
                "lines": len(code_text.splitlines()),
                "preview": _clip_preview(code_text, head=240, tail=80),
            }
            continue
        if name == "cases" and isinstance(value, list):
            compact["cases"] = [_compact_case_item(one) for one in value[:2]]
            compact["case_count"] = len(value)
            continue
        compact[name] = value
    return compact


def _compact_case_item(case: Any) -> Any:
    if not isinstance(case, dict):
        return case
    one = dict(case)
    if "stdin" in one:
        one["stdin"] = _clip_preview(one.get("stdin"), head=160, tail=40)
    return one


def _sha256_text(text: str) -> str:
    """代码指纹（trace 用）。搬移时补上缺失实现：旧文件只调用未定义，
    异常被 _append_trace 的 try/except 吞掉，导致带 code 的 action_payload 从不落日志。"""
    return "sha256:" + sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _clip_preview(raw: Any, *, head: int, tail: int) -> str:
    text = str(raw or "")
    if len(text) <= head + tail + 20:
        return text
    return text[:head] + "\n...\n" + text[-tail:]
