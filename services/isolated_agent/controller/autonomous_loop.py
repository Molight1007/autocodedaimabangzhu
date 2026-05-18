"""Isolated Agent 阶段 5：自治循环控制器。

核心职责：
- 驱动 “模型决策 -> 动作分发 -> 结果回填” 的闭环迭代。
- 在不保存代码历史的前提下，利用固定题目卡 + 动态短上下文完成多轮修复。
- 内置防死循环控制（最大尝试次数、协议错误阈值、无进展阈值）。

不负责：
- 不实现代码执行细节（由 runtime executor 注入 dispatcher）。
- 不管理多会话池；调用方持有并传入单个会话状态对象。

数据流：
- `problem + state.recent_events` -> 组装上下文 -> LLM 输出动作 JSON ->
  dispatcher 执行动作 -> 结果摘要写回上下文 -> 下一轮。

约束：
- 默认最多尝试 15 次，可通过配置覆盖。
- 对外只返回最终摘要与 final_code，不暴露隐藏测试细节。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..contracts.protocol import (
    ACTION_FINALIZE,
    ACTION_MODIFY,
    ACTION_READ,
    ACTION_RUN,
    ACTION_SELF_TEST,
    DEFAULT_CONTEXT_ROUNDS,
    DEFAULT_MAX_AUTONOMOUS_ATTEMPTS,
    MAX_CONTEXT_ROUNDS,
    RESULT_FINALIZE_OK,
    RESULT_MODIFY_OK,
    RESULT_PROTOCOL_ERROR,
)
from ..session.state import AgentSessionState
from ..verifier.agent_utils import append_agent_trace_code_version, append_agent_trace_message
from .context_tools import (
    _find_latest_read_preview,
    build_dynamic_context,
    build_problem_card,
    trim_recent_events,
)
from .dispatcher import ActionDispatcher, RunExecutor

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
    problem_card_max_chars: int = 1800
    on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    should_stop: Optional[Callable[[], bool]] = None


def run_autonomous_loop(
    *,
    state: AgentSessionState,
    problem: Dict[str, Any],
    llm_caller: LlmCaller,
    dispatcher: ActionDispatcher,
    config: Optional[LoopConfig] = None,
) -> Dict[str, Any]:
    cfg = config or LoopConfig()
    max_attempts = max(1, int(cfg.max_attempts or DEFAULT_MAX_AUTONOMOUS_ATTEMPTS))
    max_protocol_errors = max(1, int(cfg.max_protocol_errors or 3))
    max_no_progress_rounds = max(1, int(cfg.max_no_progress_rounds or 5))
    context_rounds = min(MAX_CONTEXT_ROUNDS, max(1, int(cfg.context_rounds or DEFAULT_CONTEXT_ROUNDS)))

    protocol_errors = 0
    no_progress_rounds = 0
    stop_reason_override = ""
    steps: List[Dict[str, Any]] = []
    _append_trace(
        state,
        "loop_start",
        {
            "max_attempts": max_attempts,
            "max_protocol_errors": max_protocol_errors,
            "max_no_progress_rounds": max_no_progress_rounds,
            "context_rounds": context_rounds,
            "problem_card_max_chars": int(cfg.problem_card_max_chars or 1800),
        },
    )

    for attempt in range(1, max_attempts + 1):
        if _should_stop(cfg):
            _append_trace(state, "loop_stopped", {"attempt": attempt, "reason": "interruption_requested"})
            break
        messages, _ = _build_messages(
            problem=problem,
            state=state,
            attempt_idx=attempt,
            config=cfg,
            context_rounds=context_rounds,
        )
        llm_output = llm_caller(messages)
        if _should_stop(cfg):
            _append_trace(state, "loop_stopped", {"attempt": attempt, "reason": "interruption_requested_after_llm"})
            break
        action_payload = _normalize_llm_action(llm_output)
        if action_payload:
            _append_trace(
                state,
                "action_payload",
                {
                    "attempt": attempt,
                    "payload": dict(action_payload or {}),
                },
            )
        if not action_payload:
            protocol_errors += 1
            state.push_event(
                action="invalid",
                result_type=RESULT_PROTOCOL_ERROR,
                note="llm_action_parse_failed",
            )
            _append_trace(
                state,
                "protocol_error",
                {
                    "attempt": attempt,
                    "reason": "llm_action_parse_failed",
                    "protocol_errors": protocol_errors,
                },
            )
            steps.append(
                {
                    "attempt": attempt,
                    "action": "invalid",
                    "type": RESULT_PROTOCOL_ERROR,
                    "note": "llm_action_parse_failed",
                }
            )
            if protocol_errors >= max_protocol_errors:
                break
            continue

        before_hash = str(state.code_hash)
        result = dispatcher.dispatch(action_payload, state)
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
        step_info = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "action": str(action_payload.get("action") or ""),
            "action_payload": dict(action_payload or {}),
            "type": result_type,
            "version": int(state.version),
            "code": state.current_code,
            "result": dict(result or {}),  # 完整运行结果，供 UI 展示 stdout/stderr 等
        }
        steps.append(
            {
                "attempt": attempt,
                "action": str(action_payload.get("action") or ""),
                "type": result_type,
                "version": int(state.version),
            }
        )
        if _should_stop(cfg):
            _append_trace(state, "loop_stopped", {"attempt": attempt, "reason": "interruption_requested_after_dispatch"})
            break
        if cfg.on_step_callback:
            try:
                cfg.on_step_callback(step_info)
            except Exception:
                pass

        if result_type == RESULT_FINALIZE_OK:
            _append_trace(state, "loop_success", {"attempt": attempt, "reason": "finalize_ok"})
            return {
                "passed": True,
                "session_id": state.session_id,
                "attempts": attempt,
                "final_code": state.current_code,
                "summary": {
                    "status": "ok",
                    "type": result_type,
                    "recent_events": list(state.recent_events),
                    "steps": steps,
                },
            }

        if result_type == RESULT_PROTOCOL_ERROR:
            if protocol_error_code == "data_sample_too_complex":
                stop_reason_override = "data_sample_manual_required"
                _append_trace(
                    state,
                    "loop_stop_reason",
                    {
                        "attempt": attempt,
                        "reason": stop_reason_override,
                        "protocol_error_code": protocol_error_code,
                    },
                )
                break
            protocol_errors += 1
            if protocol_errors >= max_protocol_errors:
                break
        else:
            protocol_errors = 0

        after_hash = str(state.code_hash)
        action_name = str(action_payload.get("action") or "")
        if action_name == ACTION_MODIFY and before_hash != after_hash:
            no_progress_rounds = 0
        elif action_name in {ACTION_RUN, ACTION_MODIFY, ACTION_SELF_TEST}:
            no_progress_rounds += 1

        if no_progress_rounds >= max_no_progress_rounds:
            _append_trace(
                state,
                "loop_stop_reason",
                {
                    "attempt": attempt,
                    "reason": "no_progress_limit_reached",
                    "no_progress_rounds": no_progress_rounds,
                },
            )
            break

        if action_name == ACTION_FINALIZE and result_type != RESULT_FINALIZE_OK:
            no_progress_rounds += 1

    _append_trace(
        state,
        "loop_failed",
        {
            "attempts": min(max_attempts, len(steps)),
            "protocol_errors": protocol_errors,
            "no_progress_rounds": no_progress_rounds,
        },
    )
    return {
        "passed": False,
        "session_id": state.session_id,
        "attempts": min(max_attempts, len(steps)),
        "final_code": state.current_code,
        "summary": {
            "status": "failed",
            "reason": _build_stop_reason(
                stop_reason_override=stop_reason_override,
                protocol_errors=protocol_errors,
                no_progress_rounds=no_progress_rounds,
                max_protocol_errors=max_protocol_errors,
                max_no_progress_rounds=max_no_progress_rounds,
            ),
            "recent_events": list(state.recent_events),
            "steps": steps,
        },
    }


def create_autonomous_dispatcher(*, run_executor: Optional[RunExecutor] = None) -> ActionDispatcher:
    return ActionDispatcher(run_executor=run_executor)


def _build_messages(
    *,
    problem: Dict[str, Any],
    state: AgentSessionState,
    attempt_idx: int,
    config: LoopConfig,
    context_rounds: int,
) -> tuple[List[Dict[str, str]], str]:
    """构造最小上下文。

    只包含系统规则、固定题目卡、动态短上下文；不直接注入代码全文。
    若模型需要代码，必须先调用 `read`。

    返回 (messages, dynamic_context_text) —— dynamic_context_text 仅用于日志追踪，
    不含 system_prompt 和 problem_card（两者已在 loop_start 中记录）。
    """

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
            "如果 read(target=data_sample) 返回 data_sample_too_complex，表示样本过大，需要人工提供精简样例后再继续。\n"
            "如果你需要先查看题目和代码，请先 read(target=all)；拿到内容后再给 modify 或 run。"
        )

    problem_card_max_chars = int(config.problem_card_max_chars or 1800)
    recent = trim_recent_events(state.recent_events, max_rounds=context_rounds)
    latest_read_preview = _find_latest_read_preview(recent)
    problem_text = build_problem_card(problem, max_chars=problem_card_max_chars)
    dynamic_text = build_dynamic_context(
        attempt_idx=attempt_idx,
        session_id=state.session_id,
        version=state.version,
        code_hash=state.code_hash,
        recent_events=recent,
        latest_read_preview=latest_read_preview,
        version_snapshots=state.version_snapshots,
    )
    user_text = f"{problem_text}\n\n{dynamic_text}\n请输出下一步 JSON 动作。"
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}], dynamic_text


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


def _clip_preview(raw: Any, *, head: int, tail: int) -> str:
    text = str(raw or "")
    if len(text) <= head + tail + 20:
        return text
    return text[:head] + "\n...\n" + text[-tail:]
