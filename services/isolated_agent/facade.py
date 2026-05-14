"""Isolated Agent 单入口 Facade。

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
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Dict, List, Optional

from .controller import create_session, run_autonomous_session
from .contracts.protocol import DEFAULT_CONTEXT_ROUNDS, DEFAULT_MAX_AUTONOMOUS_ATTEMPTS
from .runtime import build_default_run_executor
from .verifier.analyzer import analyze_problem
from .verifier.cross_runner import run_cross_validation
from .verifier.verdict import VerifyVerdict, create_skipped_verdict


def solve_with_agent(
    *,
    problem: Dict[str, Any],
    candidate_code: str,
    llm_caller: Callable[[list[dict[str, str]]], Any],
    runtime_context: Optional[Dict[str, Any]] = None,
    max_attempts: int = DEFAULT_MAX_AUTONOMOUS_ATTEMPTS,
    context_rounds: int = DEFAULT_CONTEXT_ROUNDS,
    session_id: str = "isolated-agent-session",
    on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """运行单题单会话的自治修复流程。"""

    state = create_session(
        session_id=str(session_id or "").strip() or "isolated-agent-session",
        candidate_code=str(candidate_code or ""),
        max_recent_events=int(context_rounds or 3),
        runtime_context=dict(runtime_context or {}),
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
        return {
            "session_id": state.session_id,
            "passed": bool((result or {}).get("passed")),
            "final_code": str((result or {}).get("final_code") or state.current_code),
            "summary": dict((result or {}).get("summary") or {}),
            "attempts": int((result or {}).get("attempts") or 0),
        }
    except Exception:
        return {
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


def verify_with_cross_agent(
    *,
    problem: dict,
    candidate_code: str,  # 已经过单 Agent 修复并 run=right 的优化代码（可选，若不传则由内部生成）
    test_cases: List[dict],  # 数据生成器提供的全部测试数据
    llm_caller_factory: Callable[[str], Callable],
    enable_cross_validation: bool = False,  # 手动开关
    max_rounds: int = 30,
    on_progress: Optional[Callable] = None,
) -> VerifyVerdict:
    """验证入口：阶段一分析 + 阶段二对拍。

    该入口为独立函数，不影响现有 solve_with_agent 行为。
    """
    final_code_fallback = str(candidate_code or "")
    if not enable_cross_validation:
        return create_skipped_verdict(reason="disabled", final_code=final_code_fallback)

    _notify_progress(on_progress, stage="analyzer_start", message="开始算法分析")
    try:
        analyzer_caller = llm_caller_factory("verify-analyzer")
        analysis = analyze_problem(
            dict(problem or {}),
            llm_caller=analyzer_caller,
        )
        analysis_dict = analysis.to_dict()
    except Exception:
        return create_skipped_verdict(
            reason="analyzer_error",
            final_code=final_code_fallback,
            summary={"error": traceback.format_exc()},
        )

    _notify_progress(on_progress, stage="cross_validation_start", message="开始双算法对拍")
    try:
        cross_result = run_cross_validation(
            problem=dict(problem or {}),
            analysis_result=analysis_dict,
            test_cases=list(test_cases or []),
            llm_caller_factory=llm_caller_factory,
            max_rounds=max_rounds,
            on_progress=on_progress,
        )
    except Exception:
        return create_skipped_verdict(
            reason="cross_validation_error",
            final_code=final_code_fallback,
            summary={"analysis": analysis_dict, "error": traceback.format_exc()},
        )

    strategy = _map_strategy(str((cross_result or {}).get("strategy") or ""))
    verification_passed = bool((cross_result or {}).get("verification_passed"))
    skip_reason = str((cross_result or {}).get("skip_reason") or "")

    code_a = str((cross_result or {}).get("code_a") or "")
    code_b = str((cross_result or {}).get("code_b") or "")
    can_brute = bool(analysis_dict.get("can_brute_force"))
    a_success = bool((cross_result or {}).get("a_success"))
    b_success = bool((cross_result or {}).get("b_success"))
    brute_force_code = code_a if can_brute else ""
    optimized_code = code_b if code_b else (code_a if not can_brute else "")

    if verification_passed:
        final_code = optimized_code or final_code_fallback
    else:
        final_code = _pick_degraded_final_code(
            can_brute_force=can_brute,
            code_a=code_a,
            code_b=code_b,
            a_success=a_success,
            b_success=b_success,
            fallback_code=final_code_fallback,
        )

    mismatch_cases = _normalize_mismatch_cases((cross_result or {}).get("mismatch_cases"))
    summary = {
        "reason": str((cross_result or {}).get("reason") or ""),
        "total_cases": int((cross_result or {}).get("total_cases") or 0),
        "matched_cases": int((cross_result or {}).get("matched_cases") or 0),
        "a_rounds": int((cross_result or {}).get("a_rounds") or 0),
        "b_rounds": int((cross_result or {}).get("b_rounds") or 0),
        "a_success": a_success,
        "b_success": b_success,
        "analysis": analysis_dict,
        "error": str((cross_result or {}).get("error") or ""),
        "selected_final_code_source": _resolve_selected_code_source(
            can_brute_force=can_brute,
            code_a=code_a,
            code_b=code_b,
            a_success=a_success,
            b_success=b_success,
            fallback_code=final_code_fallback,
            selected_code=final_code,
        ),
    }

    _notify_progress(on_progress, stage="cross_validation_done", message="对拍完成")
    return VerifyVerdict(
        verification_passed=verification_passed,
        final_code=final_code,
        brute_force_code=brute_force_code,
        optimized_code=optimized_code,
        strategy_used=strategy,
        skip_reason=skip_reason,
        mismatch_cases=mismatch_cases,
        summary=summary,
    )


def _notify_progress(
    on_progress: Optional[Callable],
    *,
    stage: str,
    message: str,
) -> None:
    if on_progress is None:
        return
    try:
        on_progress(
            {
                "stage": str(stage or ""),
                "message": str(message or ""),
            }
        )
    except Exception:
        return


def _normalize_mismatch_cases(raw_cases: Any) -> List[Dict]:
    normalized: List[Dict] = []
    for item in list(raw_cases or []):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "stdin": str(item.get("stdin") or ""),
                "output_a": str(item.get("out_a") or item.get("output_a") or ""),
                "output_b": str(item.get("out_b") or item.get("output_b") or ""),
            }
        )
    return normalized


def _map_strategy(strategy: str) -> str:
    text = str(strategy or "").strip().lower()
    if text == "brute_force":
        return "brute_force"
    if text == "cross_algo":
        return "cross_algo"
    return "skip"


def _pick_degraded_final_code(
    *,
    can_brute_force: bool,
    code_a: str,
    code_b: str,
    a_success: bool,
    b_success: bool,
    fallback_code: str,
) -> str:
    if can_brute_force:
        if b_success and code_b:
            return code_b
        if a_success and code_a:
            return code_a
    else:
        if b_success and code_b:
            return code_b
        if a_success and code_a:
            return code_a

    if code_b:
        return code_b
    if code_a:
        return code_a
    return str(fallback_code or "")


def _resolve_selected_code_source(
    *,
    can_brute_force: bool,
    code_a: str,
    code_b: str,
    a_success: bool,
    b_success: bool,
    fallback_code: str,
    selected_code: str,
) -> str:
    selected = str(selected_code or "")
    if selected and b_success and selected == str(code_b or ""):
        return "optimized_agent" if can_brute_force else "agent_b"
    if selected and a_success and selected == str(code_a or ""):
        return "brute_force_agent" if can_brute_force else "agent_a"
    if selected and selected == str(fallback_code or ""):
        return "candidate_fallback"
    if selected and selected == str(code_b or ""):
        return "agent_b_partial"
    if selected and selected == str(code_a or ""):
        return "agent_a_partial"
    return "unknown"
