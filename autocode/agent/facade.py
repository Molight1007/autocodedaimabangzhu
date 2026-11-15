"""autoCode Agent 单入口 Facade。

核心职责：
- 对外提供一个稳定入口，封装会话创建、自治循环执行、返回结果标准化。
- 统一默认参数（最大尝试次数、上下文轮数、运行上下文），降低接入方复杂度。
- 在入口层拦截异常并返回结构化失败结果，避免调用方处理内部异常细节。

不负责：
- 不直接实现动作分发与执行逻辑（委托 controller/runtime）。
- 不管理多会话持久化（调用方可按需在外层维护）。

数据流：
- 外部请求 -> `solve_with_agent` -> create_session ->
  run_autonomous_session -> 返回 `passed/final_code/summary/session_id`。

P0 迁移说明（差异均为设计 §2 判定的删减，非行为改动）：
- 删除 `verify_with_cross_agent`：其依赖的 `verifier/cross_runner.py`（1045 行）与
  `verifier/data_pool.py` 按设计整体弃用；对拍思想将由 `engine/verify/cross_check.py`
  重写（P1/P2），届时另开入口。
- 因此同样删除只为对拍服务的 `_notify_progress` / `_map_strategy` /
  `_pick_degraded_final_code` / `_resolve_selected_code_source` /
  `_normalize_mismatch_cases` 五个私有辅助函数。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Dict, List, Optional

from .contracts.protocol import DEFAULT_CONTEXT_ROUNDS, DEFAULT_MAX_AUTONOMOUS_ATTEMPTS
from .controller import create_session, run_autonomous_session
from .runtime import build_default_run_executor
from .trace import (
    append_agent_trace_code_version,
    append_agent_trace_message,
    finalize_agent_trace_run,
    start_agent_trace_run,
)


def solve_with_agent(
    *,
    problem: Dict[str, Any],
    candidate_code: str,
    llm_caller: Callable[[List[Dict[str, str]]], Any],
    runtime_context: Optional[Dict[str, Any]] = None,
    max_attempts: int = DEFAULT_MAX_AUTONOMOUS_ATTEMPTS,
    context_rounds: int = DEFAULT_CONTEXT_ROUNDS,
    session_id: str = "isolated-agent-session",
    on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """运行单题单会话的自治修复流程。"""
    trace_context = dict(runtime_context or {})
    trace_disabled = bool(trace_context.get("disable_trace_store"))
    trace_run_id = str(trace_context.get("trace_run_id") or "").strip()
    owns_trace_run = False
    if not trace_disabled and not trace_run_id:
        trace_run_id = start_agent_trace_run(
            problem=dict(problem or {}),
            mode="single_agent",
            meta={
                "session_id": str(session_id or "").strip() or "isolated-agent-session",
                "max_attempts": int(max_attempts or DEFAULT_MAX_AUTONOMOUS_ATTEMPTS),
                "context_rounds": int(context_rounds or DEFAULT_CONTEXT_ROUNDS),
            },
        )
        if trace_run_id:
            owns_trace_run = True
            trace_context["trace_run_id"] = trace_run_id
            append_agent_trace_message(
                trace_run_id,
                {
                    "stage": "session_start",
                    "message": "创建单 Agent 修复会话",
                    "mode": "single_agent",
                },
            )
            if str(candidate_code or ""):
                append_agent_trace_code_version(
                    trace_run_id,
                    slot="single_agent",
                    phase="initial",
                    code=str(candidate_code or ""),
                    success=False,
                    rounds=0,
                )

    state = create_session(
        session_id=str(session_id or "").strip() or "isolated-agent-session",
        candidate_code=str(candidate_code or ""),
        max_recent_events=int(context_rounds or 3),
        runtime_context=trace_context,
    )
    executor = build_default_run_executor()
    try:
        result = run_autonomous_session(
            state=state,
            problem=dict(problem or {}),
            llm_caller=llm_caller,
            run_executor=executor,
            max_attempts=max_attempts,
            context_rounds=context_rounds,
            on_step_callback=on_step_callback,
            should_stop=should_stop,
        )
        response = {
            "session_id": state.session_id,
            "passed": bool((result or {}).get("passed")),
            "final_code": str((result or {}).get("final_code") or state.current_code),
            "summary": dict((result or {}).get("summary") or {}),
            "attempts": int((result or {}).get("attempts") or 0),
        }
        if owns_trace_run and trace_run_id:
            final_code = str(response.get("final_code") or "")
            if final_code:
                append_agent_trace_code_version(
                    trace_run_id,
                    slot="single_agent",
                    phase="final",
                    code=final_code,
                    success=bool(response.get("passed")),
                    rounds=int(response.get("attempts") or 0),
                )
            finalize_agent_trace_run(
                trace_run_id,
                {
                    "passed": bool(response.get("passed")),
                    "reason": str(
                        dict(response.get("summary") or {}).get("reason")
                        or ("finalize_ok" if response.get("passed") else "autonomous_failed")
                    ),
                    "attempts": int(response.get("attempts") or 0),
                    "session_id": str(response.get("session_id") or ""),
                    "summary": dict(response.get("summary") or {}),
                },
                status="done",
            )
        return response
    except Exception:
        response = {
            "session_id": state.session_id,
            "passed": False,
            "final_code": state.current_code,
            "attempts": 0,
            "summary": {
                "status": "failed",
                "reason": "facade_runtime_exception",
                "error": traceback.format_exc(),
            },
        }
        if owns_trace_run and trace_run_id:
            if str(state.current_code or ""):
                append_agent_trace_code_version(
                    trace_run_id,
                    slot="single_agent",
                    phase="final",
                    code=str(state.current_code or ""),
                    success=False,
                    rounds=0,
                )
            finalize_agent_trace_run(
                trace_run_id,
                {
                    "passed": False,
                    "reason": "facade_runtime_exception",
                    "attempts": 0,
                    "session_id": str(state.session_id or ""),
                    "summary": dict(response.get("summary") or {}),
                },
                status="done",
            )
        return response
