"""Isolated Agent 对外控制层入口。

核心职责：
- 提供可直接接入的最小 API：创建会话、分发动作、自治循环。
- 隔离调用方与内部类实现，后续替换 dispatcher 时保持外部接口稳定。

上下文双规机制（本文件负责参数落地，不负责提示词拼装）：
- 规则 A（题目注入）：首轮由循环层注入完整题目；后续轮次仅使用题目摘要，信息不足时由模型显式调用 `read(target=problem/all)` 自取，复杂数据可调用 `read(target=data_sample)`。
- 规则 B（窗口裁剪）：会话仅保留最近 N 轮事件摘要（默认 15，最大 30），避免长历史占满上下文窗口。
- 本文件通过 `context_rounds/max_recent_events` 将窗口配置下沉到状态与循环层，确保行为一致。

不负责：
- 不持有全局会话池；调用方需自行管理 session 生命周期。
- 不存储代码历史与长对话历史；仅维护当前权威代码与短摘要窗口。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..contracts.protocol import DEFAULT_CONTEXT_ROUNDS, DEFAULT_MAX_AUTONOMOUS_ATTEMPTS, MAX_CONTEXT_ROUNDS
from ..runtime import build_default_run_executor
from ..session.state import AgentSessionState
from .autonomous_loop import LoopConfig, create_autonomous_dispatcher, run_autonomous_loop
from .dispatcher import ActionDispatcher, RunExecutor


def create_session(
    *,
    session_id: str,
    candidate_code: str,
    max_recent_events: int = DEFAULT_CONTEXT_ROUNDS,
    runtime_context: Optional[Dict[str, Any]] = None,
) -> AgentSessionState:
    state = AgentSessionState(
        session_id=str(session_id or "").strip() or "isolated-agent-session",
        current_code=str(candidate_code or ""),
        max_recent_events=min(MAX_CONTEXT_ROUNDS, max(1, int(max_recent_events or DEFAULT_CONTEXT_ROUNDS))),
    )
    if isinstance(runtime_context, dict) and runtime_context:
        state.runtime_context = dict(runtime_context)
    return state


def dispatch_action(
    *,
    payload: Dict[str, Any],
    state: AgentSessionState,
    run_executor: Optional[RunExecutor] = None,
) -> Dict[str, Any]:
    executor = run_executor or build_default_run_executor()
    dispatcher = ActionDispatcher(run_executor=executor)
    return dispatcher.dispatch(payload, state)


def run_autonomous_session(
    *,
    state: AgentSessionState,
    problem: Dict[str, Any],
    llm_caller,
    run_executor: Optional[RunExecutor] = None,
    max_attempts: int = DEFAULT_MAX_AUTONOMOUS_ATTEMPTS,
    context_rounds: int = DEFAULT_CONTEXT_ROUNDS,
    on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    if "problem" not in state.runtime_context:
        state.runtime_context["problem"] = dict(problem or {})

    dispatcher = create_autonomous_dispatcher(run_executor=run_executor or build_default_run_executor())
    rounds = min(MAX_CONTEXT_ROUNDS, max(1, int(context_rounds or DEFAULT_CONTEXT_ROUNDS)))
    config = LoopConfig(
        max_attempts=max_attempts,
        context_rounds=rounds,
        on_step_callback=on_step_callback,
        should_stop=should_stop,
    )
    return run_autonomous_loop(
        state=state,
        problem=dict(problem or {}),
        llm_caller=llm_caller,
        dispatcher=dispatcher,
        config=config,
    )
