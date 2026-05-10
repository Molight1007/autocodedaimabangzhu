"""Isolated Agent 阶段 5：自治循环控制器。

核心职责：
- 驱动 “模型决策 -> 动作分发 -> 结果回填” 的闭环迭代。
- 在不保存代码历史的前提下，仅利用会话权威代码与最近摘要完成多轮修复。
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
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
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
    RESULT_PROTOCOL_ERROR,
)
from ..session.state import AgentSessionState
from .context_tools import build_user_context
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
    problem_summary_max_chars: int = 1500
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
            "problem_summary_max_chars": int(cfg.problem_summary_max_chars or 1500),
        },
    )

    for attempt in range(1, max_attempts + 1):
        if _should_stop(cfg):
            _append_trace(state, "loop_stopped", {"attempt": attempt, "reason": "interruption_requested"})
            break
        messages = _build_messages(
            problem=problem,
            state=state,
            attempt_idx=attempt,
            config=cfg,
            context_rounds=context_rounds,
        )
        _append_trace(
            state,
            "attempt_begin",
            {
                "attempt": attempt,
                "version": int(state.version),
                "code_hash": str(state.code_hash),
                "messages": _clip_messages_for_trace(messages),
            },
        )
        llm_output = llm_caller(messages)
        _append_trace(state, "llm_output", {"attempt": attempt, "raw": _clip_text_for_trace(llm_output)})
        if _should_stop(cfg):
            _append_trace(state, "loop_stopped", {"attempt": attempt, "reason": "interruption_requested_after_llm"})
            break
        action_payload = _normalize_llm_action(llm_output)
        _append_trace(state, "action_payload", {"attempt": attempt, "payload": dict(action_payload or {})})
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
                "result": _clip_json_like(result),
            },
        )
        step_info = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "action": str(action_payload.get("action") or ""),
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
) -> List[Dict[str, str]]:
    """构造最小上下文。

    只包含系统规则、题目描述、最近 N 条摘要；不直接注入代码全文。
    若模型需要代码，必须先调用 `read`。
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
    user_text = build_user_context(
        attempt_idx=attempt_idx,
        problem=problem,
        session_id=state.session_id,
        version=state.version,
        code_hash=state.code_hash,
        events=state.recent_events,
        max_rounds=context_rounds,
        problem_summary_max_chars=int(config.problem_summary_max_chars or 1500),
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}]


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


def _resolve_trace_log_path(state: AgentSessionState) -> Path:
    custom = str((state.runtime_context or {}).get("trace_log_path") or "").strip()
    if custom:
        return Path(custom)
    root_dir = Path(__file__).resolve().parents[3]
    return root_dir / "logs" / "autonomous_trace.jsonl"


def _append_trace(state: AgentSessionState, event: str, payload: Dict[str, Any]) -> None:
    try:
        log_path = _resolve_trace_log_path(state)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": str(event or ""),
            "session_id": str(state.session_id or ""),
            "version": int(state.version),
            "code_hash": str(state.code_hash or ""),
            "payload": _clip_json_like(payload),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return


def _clip_messages_for_trace(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in list(messages or []):
        item = dict(m or {})
        out.append(
            {
                "role": str(item.get("role") or ""),
                "content": _clip_text_for_trace(item.get("content") or ""),
            }
        )
    return out


def _clip_text_for_trace(raw: Any, limit: int = 6000) -> str:
    text = str(raw or "")
    lim = max(500, int(limit or 6000))
    if len(text) <= lim:
        return text
    head = int(lim * 0.7)
    tail = lim - head
    return text[:head] + "\n...\n(trace truncated)\n...\n" + text[-tail:]


def _clip_json_like(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clip_json_like(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip_json_like(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str):
            return _clip_text_for_trace(obj)
        return obj
    return _clip_text_for_trace(obj)
