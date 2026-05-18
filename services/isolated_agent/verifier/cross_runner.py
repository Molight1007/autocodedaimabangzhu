"""双代码对拍验证编排器。

负责：
- 路由降级判断（output_type / confidence / algorithms 不足时走 skip）。
- 并发双 Agent 代码生成 + 自治修复。
- 数据池过滤后对拍比对 stdout。
- 不匹配时注入交叉反馈重试，最终产出验证裁决。

子模块依赖：
- agent_utils/prompts.py : 算法约束 prompt / 会话标签 / 反馈 payload / 代码骨架。
- agent_utils/trace.py   : 问题级追踪日志聚合 / 单问题归档。
- data_pool.py           : 测试数据长度 + 数量过滤。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .agent_utils import (
    CROSS_FEEDBACK_INSTRUCTION,
    append_agent_trace_code_version,
    append_agent_trace_message,
    bootstrap_candidate_code,
    build_algorithm_system_prompt,
    build_feedback_payload,
    build_session_tag,
    finalize_agent_trace_run,
    start_agent_trace_run,
)
from ..contracts.protocol import (
    RESULT_GRAMMATICAL_ERROR,
    RESULT_RIGHT,
    RESULT_RUNTIME_ERROR,
    RESULT_SYSTEM_ERROR,
    RESULT_WRONG,
    VERIFY_CONFIDENCE_LOW,
    VERIFY_OUTPUT_ANY,
    VERIFY_STRATEGY_BRUTE,
    VERIFY_STRATEGY_CROSS,
    VERIFY_STRATEGY_SKIP,
)
from ..runtime.executor import build_default_run_executor
from ..session.state import AgentSessionState
from .data_pool import prepare_cross_validation_pool


@dataclass(frozen=True)
class _SingleGenResult:
    code: str
    success: bool
    rounds: int
    error: str


@dataclass(frozen=True)
class _ExecOutcome:
    run_type: str
    output: str
    error: str
    is_timeout: bool


def _safe_report_progress(
    on_progress: Optional[Callable[[Dict[str, Any]], None]],
    payload: Dict[str, Any],
    trace_run_id: str = "",
) -> None:
    if trace_run_id and _should_capture_progress_message(payload):
        one = dict(payload or {})
        one.setdefault("mode", "cross_validation")
        append_agent_trace_message(trace_run_id, one)
    if on_progress is None:
        return
    try:
        on_progress(payload)
    except Exception:
        pass


def _should_capture_progress_message(payload: Dict[str, Any]) -> bool:
    stage = str((payload or {}).get("stage") or "")
    return stage != "compare_progress"


def generate_dual_codes(
    *,
    problem: dict,
    algorithms: list,  # 阶段一输出的 algorithms 数组前两个元素
    llm_caller_factory: Callable[[str], Callable],
    max_rounds: int = 30,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    trace_run_id: str = "",
) -> dict:
    """并发生成两份相互隔离的候选代码。

    Args:
        problem: 题目对象（dict）。
        algorithms: 算法列表，至少 2 项，支持 dict/object/str 三种形态。
        llm_caller_factory: 会话工厂，输入 session tag，返回该会话专属 llm_caller。
        max_rounds: 每个 Agent 的最大自治轮数。

    Returns:
        {
            "code_a": str,
            "code_b": str,
            "a_success": bool,
            "b_success": bool,
            "a_rounds": int,
            "b_rounds": int,
            "a_error": str,
            "b_error": str
        }
    """
    rounds = max(1, int(max_rounds or 30))
    pb = dict(problem or {})
    default_result = {
        "code_a": "",
        "code_b": "",
        "a_success": False,
        "b_success": False,
        "a_rounds": 0,
        "b_rounds": 0,
        "a_error": "",
        "b_error": "",
    }

    if not callable(llm_caller_factory):
        default_result["a_error"] = "llm_caller_factory_not_callable"
        default_result["b_error"] = "llm_caller_factory_not_callable"
        return default_result

    if len(list(algorithms or [])) < 2:
        default_result["a_error"] = "algorithms_requires_at_least_2_items"
        default_result["b_error"] = "algorithms_requires_at_least_2_items"
        return default_result

    algo_a = _normalize_algorithm(algorithms[0], fallback_name="algorithm_a")
    algo_b = _normalize_algorithm(algorithms[1], fallback_name="algorithm_b")

    _safe_report_progress(
        on_progress,
        {"stage": "dual_gen_start", "message": f"Agent-A[{algo_a['name']}] vs Agent-B[{algo_b['name']}] 并发生成中"},
        trace_run_id=trace_run_id,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            _run_single_agent,
            problem=pb,
            algorithm=algo_a,
            llm_caller_factory=llm_caller_factory,
            max_rounds=rounds,
            slot="a",
            on_progress=on_progress,
            trace_run_id=trace_run_id,
        )
        fut_b = pool.submit(
            _run_single_agent,
            problem=pb,
            algorithm=algo_b,
            llm_caller_factory=llm_caller_factory,
            max_rounds=rounds,
            slot="b",
            on_progress=on_progress,
            trace_run_id=trace_run_id,
        )
        res_a = fut_a.result()
        res_b = fut_b.result()
    a_status = "通过" if res_a.success else f"失败({res_a.error})"
    b_status = "通过" if res_b.success else f"失败({res_b.error})"
    _safe_report_progress(
        on_progress,
        {"stage": "dual_gen_done", "message": f"Agent-A: {a_status} ({res_a.rounds}轮), Agent-B: {b_status} ({res_b.rounds}轮)"},
        trace_run_id=trace_run_id,
    )

    return {
        "code_a": res_a.code,
        "code_b": res_b.code,
        "a_success": res_a.success,
        "b_success": res_b.success,
        "a_rounds": res_a.rounds,
        "b_rounds": res_b.rounds,
        "a_error": res_a.error,
        "b_error": res_b.error,
    }


def run_cross_validation(
    *,
    problem: dict,
    analysis_result: dict,      # 阶段一输出
    test_cases: list,           # 全部测试数据，每个元素含 stdin
    llm_caller_factory: Callable,
    max_rounds: int = 30,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> dict:
    """执行双代码对拍与一次交叉反馈重试。"""
    pb = dict(problem or {})
    analysis = dict(analysis_result or {})
    rounds = max(1, int(max_rounds or 30))
    algorithms = list(analysis.get("algorithms") or [])
    strategy = (
        VERIFY_STRATEGY_BRUTE if bool(analysis.get("can_brute_force")) else VERIFY_STRATEGY_CROSS
    )
    trace_run_id = start_agent_trace_run(
        problem=pb,
        mode="cross_validation",
        meta={
            "strategy": strategy,
            "max_rounds": rounds,
            "test_case_count": len(list(test_cases or [])),
            "confidence": str(analysis.get("confidence") or ""),
            "output_type": str(analysis.get("output_type") or ""),
            "analysis": analysis,
        },
    )

    _safe_report_progress(
        on_progress,
        {
            "stage": "cross_validation_start",
            "message": "开始双算法对拍",
        },
        trace_run_id=trace_run_id,
    )

    verdict = _try_early_degrade(analysis=analysis, algorithms=algorithms, test_cases=test_cases)
    if verdict is not None:
        finalize_agent_trace_run(trace_run_id, verdict, status="done")
        return verdict

    result = _generate_and_first_compare(
        pb=pb, algorithms=algorithms, analysis=analysis,
        test_cases=test_cases, rounds=rounds, strategy=strategy,
        llm_caller_factory=llm_caller_factory, on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    if "verdict" in result:
        finalize_agent_trace_run(trace_run_id, result["verdict"], status="done")
        return result["verdict"]

    verdict = _retry_and_second_compare(
        pb=pb, algorithms=algorithms, dual=result["dual"],
        pool=result["pool"], first_compare=result["first_compare"],
        analysis=analysis, rounds=rounds, strategy=strategy,
        llm_caller_factory=llm_caller_factory, on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    finalize_agent_trace_run(trace_run_id, verdict, status="done")
    return verdict


def _try_early_degrade(
    *,
    analysis: Dict[str, Any],
    algorithms: list,
    test_cases: list,
) -> Optional[dict]:
    """无需 LLM 调用的前置降级判断。

    Returns:
        降级 verdict dict 或 None（继续）。
    """
    if str(analysis.get("output_type") or "").strip() == VERIFY_OUTPUT_ANY:
        return _build_verdict(passed=False,
            strategy=VERIFY_STRATEGY_SKIP,
            analysis=analysis,
            reason="output_not_unique",
            total_cases=len(list(test_cases or [])),
            matched_cases=0,
            cases=(),
        )

    if (
        str(analysis.get("confidence") or "").strip() == VERIFY_CONFIDENCE_LOW
        or len(algorithms) < 2
    ):
        return _build_verdict(passed=False,
            strategy=VERIFY_STRATEGY_SKIP,
            analysis=analysis,
            reason="low_confidence",
            total_cases=len(list(test_cases or [])),
            matched_cases=0,
            cases=(),
            error=f"algorithms_count={len(algorithms)}",
        )

    return None


def _generate_and_first_compare(
    *,
    pb: Dict[str, Any],
    algorithms: list,
    analysis: Dict[str, Any],
    test_cases: list,
    rounds: int,
    strategy: str,
    llm_caller_factory: Callable,
    on_progress: Optional[Callable[[Dict[str, Any]], None]],
    trace_run_id: str,
) -> Dict[str, Any]:
    """双代码生成 → 数据池过滤 → 首次对拍。

    Returns:
        {"verdict": dict}  ← 已产生最终裁决。
        {"dual": ..., "pool": ..., "first_compare": ...}  ← 继续重试。
    """
    dual = generate_dual_codes(
        problem=pb,
        algorithms=algorithms[:2],
        llm_caller_factory=llm_caller_factory,
        max_rounds=rounds,
        on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    append_agent_trace_message(
        trace_run_id,
        {
            "stage": "dual_codes_generated",
            "message": "双算法代码生成完成",
            "mode": "cross_validation",
            "details": {
                "a_success": bool(dual.get("a_success")),
                "b_success": bool(dual.get("b_success")),
                "a_rounds": int(dual.get("a_rounds") or 0),
                "b_rounds": int(dual.get("b_rounds") or 0),
                "a_error": str(dual.get("a_error") or ""),
                "b_error": str(dual.get("b_error") or ""),
            },
        },
    )
    append_agent_trace_code_version(
        trace_run_id,
        slot="a",
        phase="dual_generated",
        code=str(dual.get("code_a") or ""),
        success=bool(dual.get("a_success")),
        rounds=int(dual.get("a_rounds") or 0),
        error=str(dual.get("a_error") or ""),
    )
    append_agent_trace_code_version(
        trace_run_id,
        slot="b",
        phase="dual_generated",
        code=str(dual.get("code_b") or ""),
        success=bool(dual.get("b_success")),
        rounds=int(dual.get("b_rounds") or 0),
        error=str(dual.get("b_error") or ""),
    )

    if not bool(dual.get("a_success")) or not bool(dual.get("b_success")):
        return {"verdict": _build_verdict(passed=False,
            strategy=VERIFY_STRATEGY_SKIP,
            analysis=analysis,
            reason="dual_generation_failed",
            total_cases=len(list(test_cases or [])),
            matched_cases=0,
            cases=(),
            error=f"a_error={dual.get('a_error')}; b_error={dual.get('b_error')}",
            code_a=str(dual.get("code_a") or ""),
            code_b=str(dual.get("code_b") or ""),
            a_success=bool(dual.get("a_success")),
            b_success=bool(dual.get("b_success")),
            a_rounds=int(dual.get("a_rounds") or 0),
            b_rounds=int(dual.get("b_rounds") or 0),
        )}

    pool = prepare_cross_validation_pool(test_cases)
    append_agent_trace_message(
        trace_run_id,
        {
            "stage": "data_pool_filtered",
            "message": f"数据池过滤完成，保留 {pool.total_retained}/{pool.total_original} 条",
            "mode": "cross_validation",
            "details": {
                "total_original": pool.total_original,
                "total_filtered_by_size": pool.total_filtered_by_size,
                "total_dropped_by_cap": pool.total_dropped_by_cap,
                "total_retained": pool.total_retained,
            },
        },
    )
    if pool.is_empty:
        return {"verdict": _build_verdict(passed=False,
            strategy=strategy,
            analysis=analysis,
            reason="data_pool_empty",
            total_cases=pool.total_original,
            matched_cases=0,
            cases=(),
            error=f"filtered_by_size={pool.total_filtered_by_size}",
        )}

    first_compare = _compare_codes_on_cases(
        code_a=str(dual.get("code_a") or ""),
        code_b=str(dual.get("code_b") or ""),
        test_cases=pool.cases,
        can_brute_force=bool(analysis.get("can_brute_force")),
        on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    append_agent_trace_message(
        trace_run_id,
        {
            "stage": "first_compare_done",
            "message": f"首轮对拍完成，匹配 {int(first_compare.get('matched_cases') or 0)}/{int(first_compare.get('total_cases') or 0)}",
            "mode": "cross_validation",
            "details": {
                "all_match": bool(first_compare.get("all_match")),
                "terminated_reason": str(first_compare.get("terminated_reason") or ""),
                "first_mismatch": dict(first_compare.get("first_mismatch") or {}),
            },
        },
    )

    terminated_reason = str(first_compare.get("terminated_reason") or "").strip()
    if terminated_reason:
        return {"verdict": _build_verdict(passed=False,
            strategy=strategy,
            analysis=analysis,
            reason=terminated_reason,
            total_cases=int(first_compare.get("total_cases") or 0),
            matched_cases=int(first_compare.get("matched_cases") or 0),
            cases=tuple(first_compare.get("cases") or ()),
            mismatch_cases=tuple(first_compare.get("mismatch_cases") or ()),
        )}

    if bool(first_compare.get("all_match")):
        return {"verdict": _build_verdict(
            passed=True,
            strategy=strategy,
            analysis=analysis,
            total_cases=int(first_compare.get("total_cases") or 0),
            matched_cases=int(first_compare.get("matched_cases") or 0),
            cases=tuple(first_compare.get("cases") or ()),
            code_a=str(dual.get("code_a") or ""),
            code_b=str(dual.get("code_b") or ""),
            a_rounds=int(dual.get("a_rounds") or 0),
            b_rounds=int(dual.get("b_rounds") or 0),
            a_success=bool(dual.get("a_success")),
            b_success=bool(dual.get("b_success")),
        )}

    first_mismatch = dict(first_compare.get("first_mismatch") or {})
    if not first_mismatch:
        return {"verdict": _build_verdict(passed=False,
            strategy=strategy,
            analysis=analysis,
            reason="no_comparable_cases",
            total_cases=int(first_compare.get("total_cases") or 0),
            matched_cases=int(first_compare.get("matched_cases") or 0),
            cases=tuple(first_compare.get("cases") or ()),
        )}

    return {"dual": dual, "pool": pool, "first_compare": first_compare}


def _retry_and_second_compare(
    *,
    pb: Dict[str, Any],
    algorithms: list,
    dual: Dict[str, Any],
    pool,
    first_compare: Dict[str, Any],
    analysis: Dict[str, Any],
    rounds: int,
    strategy: str,
    llm_caller_factory: Callable,
    on_progress: Optional[Callable[[Dict[str, Any]], None]],
    trace_run_id: str,
) -> dict:
    """交叉反馈重试 + 二次对拍，返回最终裁决。"""
    algo_a = _normalize_algorithm(algorithms[0], fallback_name="algorithm_a")
    algo_b = _normalize_algorithm(algorithms[1], fallback_name="algorithm_b")

    feedback = build_feedback_payload(dict(first_compare.get("first_mismatch") or {}))
    retry_a = _retry_single_agent_after_mismatch(
        slot="a",
        algorithm=algo_a,
        problem=pb,
        llm_caller_factory=llm_caller_factory,
        current_code=str(dual.get("code_a") or ""),
        used_rounds=int(dual.get("a_rounds") or 0),
        max_rounds=rounds,
        feedback=feedback,
        on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    retry_b = _retry_single_agent_after_mismatch(
        slot="b",
        algorithm=algo_b,
        problem=pb,
        llm_caller_factory=llm_caller_factory,
        current_code=str(dual.get("code_b") or ""),
        used_rounds=int(dual.get("b_rounds") or 0),
        max_rounds=rounds,
        feedback=feedback,
        on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    append_agent_trace_message(
        trace_run_id,
        {
            "stage": "retry_done",
            "message": "交叉反馈修复完成",
            "mode": "cross_validation",
            "details": {
                "retry_a": retry_a,
                "retry_b": retry_b,
            },
        },
    )
    append_agent_trace_code_version(
        trace_run_id,
        slot="a",
        phase="retry_done",
        code=str(retry_a.get("code") or dual.get("code_a") or ""),
        success=bool(retry_a.get("success") or dual.get("a_success")),
        rounds=int(retry_a.get("rounds") or dual.get("a_rounds") or 0),
        error=str(retry_a.get("error") or ""),
    )
    append_agent_trace_code_version(
        trace_run_id,
        slot="b",
        phase="retry_done",
        code=str(retry_b.get("code") or dual.get("code_b") or ""),
        success=bool(retry_b.get("success") or dual.get("b_success")),
        rounds=int(retry_b.get("rounds") or dual.get("b_rounds") or 0),
        error=str(retry_b.get("error") or ""),
    )

    second_compare = _compare_codes_on_cases(
        code_a=str(retry_a.get("code") or dual.get("code_a") or ""),
        code_b=str(retry_b.get("code") or dual.get("code_b") or ""),
        test_cases=pool.cases,
        can_brute_force=bool(analysis.get("can_brute_force")),
        on_progress=on_progress,
        trace_run_id=trace_run_id,
    )
    append_agent_trace_message(
        trace_run_id,
        {
            "stage": "second_compare_done",
            "message": f"第二轮对拍完成，匹配 {int(second_compare.get('matched_cases') or 0)}/{int(second_compare.get('total_cases') or 0)}",
            "mode": "cross_validation",
            "details": {
                "all_match": bool(second_compare.get("all_match")),
                "terminated_reason": str(second_compare.get("terminated_reason") or ""),
                "first_mismatch": dict(second_compare.get("first_mismatch") or {}),
            },
        },
    )

    terminated_reason = str(second_compare.get("terminated_reason") or "").strip()
    if terminated_reason:
        return _build_verdict(passed=False,
            strategy=strategy,
            analysis=analysis,
            reason=terminated_reason,
            total_cases=int(second_compare.get("total_cases") or 0),
            matched_cases=int(second_compare.get("matched_cases") or 0),
            cases=tuple(second_compare.get("cases") or ()),
            mismatch_cases=tuple(second_compare.get("mismatch_cases") or ()),
        )

    if bool(second_compare.get("all_match")):
        return _build_verdict(
            passed=True,
            strategy=strategy,
            analysis=analysis,
            total_cases=int(second_compare.get("total_cases") or 0),
            matched_cases=int(second_compare.get("matched_cases") or 0),
            cases=tuple(second_compare.get("cases") or ()),
            code_a=str(retry_a.get("code") or dual.get("code_a") or ""),
            code_b=str(retry_b.get("code") or dual.get("code_b") or ""),
            a_rounds=int(retry_a.get("rounds") or dual.get("a_rounds") or 0),
            b_rounds=int(retry_b.get("rounds") or dual.get("b_rounds") or 0),
            a_success=bool(retry_a.get("success") or dual.get("a_success")),
            b_success=bool(retry_b.get("success") or dual.get("b_success")),
        )

    return _build_verdict(
        passed=False,
        strategy=strategy,
        analysis=analysis,
        reason="mismatch_after_retry",
        total_cases=int(second_compare.get("total_cases") or 0),
        matched_cases=int(second_compare.get("matched_cases") or 0),
        cases=tuple(second_compare.get("cases") or ()),
        mismatch_cases=tuple(second_compare.get("mismatch_cases") or ()),
        code_a=str(retry_a.get("code") or dual.get("code_a") or ""),
        code_b=str(retry_b.get("code") or dual.get("code_b") or ""),
        a_success=bool(retry_a.get("success") or dual.get("a_success")),
        b_success=bool(retry_b.get("success") or dual.get("b_success")),
        a_rounds=int(retry_a.get("rounds") or dual.get("a_rounds") or 0),
        b_rounds=int(retry_b.get("rounds") or dual.get("b_rounds") or 0),
    )


def _run_single_agent(
    *,
    problem: Dict[str, Any],
    algorithm: Dict[str, str],
    llm_caller_factory: Callable[[str], Callable],
    max_rounds: int,
    slot: str,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    trace_run_id: str = "",
) -> _SingleGenResult:
    session_tag = build_session_tag(slot=slot, algorithm_name=str(algorithm.get("name") or "algorithm"))
    try:
        llm_caller = llm_caller_factory(session_tag)
        if not callable(llm_caller):
            return _SingleGenResult(
                code="",
                success=False,
                rounds=0,
                error="llm_caller_factory_returned_non_callable",
            )

        runtime_context = {
            "problem": dict(problem or {}),
            "data_samples": _extract_data_samples(problem),
            "agent_system_prompt": build_algorithm_system_prompt(
                algorithm_name=algorithm["name"],
                algorithm_desc=algorithm["description"],
            ),
            "disable_trace_store": True,
        }

        algo_name = str(algorithm.get("name") or "algorithm")

        def _on_agent_step(step_info: Dict[str, Any]) -> None:
            action = str((step_info or {}).get("action") or "unknown")
            attempt = int((step_info or {}).get("attempt") or 0)
            result_type = str((step_info or {}).get("type") or "")
            _safe_report_progress(
                on_progress,
                {
                    "stage": "agent_step",
                    "slot": slot,
                    "algorithm": algo_name,
                    "action": action,
                    "attempt": attempt,
                    "result_type": result_type,
                    "message": f"Agent-{slot.upper()}[{algo_name}] 第{attempt}轮 {action}" + (f" → {result_type}" if result_type else ""),
                    "details": {"payload": dict((step_info or {}).get("action_payload") or {})},
                },
                trace_run_id=trace_run_id,
            )

        result = _call_solve_with_agent(
            problem=dict(problem or {}),
            candidate_code=bootstrap_candidate_code(),
            llm_caller=llm_caller,
            runtime_context=runtime_context,
            max_attempts=max_rounds,
            context_rounds=min(max_rounds, 30),
            session_id=session_tag,
            on_step_callback=_on_agent_step,
        )
        passed = bool((result or {}).get("passed"))
        rounds = int((result or {}).get("attempts") or 0)
        final_code = str((result or {}).get("final_code") or "")
        if passed:
            return _SingleGenResult(code=final_code, success=True, rounds=rounds, error="")

        return _SingleGenResult(
            code=final_code,
            success=False,
            rounds=rounds,
            error=_extract_failure_reason(result),
        )
    except Exception as exc:
        return _SingleGenResult(
            code="",
            success=False,
            rounds=0,
            error=f"agent_exception: {exc}",
        )


def _normalize_algorithm(item: Any, *, fallback_name: str) -> Dict[str, str]:
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip() or fallback_name
        desc = str(item.get("description") or "").strip() or "no description provided"
        return {"name": name, "description": desc}

    name = str(getattr(item, "name", "") or "").strip()
    desc = str(getattr(item, "description", "") or "").strip()
    if name:
        return {"name": name, "description": desc or "no description provided"}

    raw = str(item or "").strip()
    if raw:
        return {"name": raw, "description": "no description provided"}
    return {"name": fallback_name, "description": "no description provided"}


def _extract_data_samples(problem: Dict[str, Any]) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    source = dict(problem or {})
    for key in ("data_samples", "samples", "cases"):
        value = source.get(key)
        if isinstance(value, list):
            for item in value:
                normalized = _normalize_sample_item(item)
                if normalized is not None:
                    samples.append(normalized)

    if any(k in source for k in ("stdin", "stdout", "vars", "meta")):
        normalized = _normalize_sample_item(source)
        if normalized is not None:
            samples.append(normalized)
    return samples


def _normalize_sample_item(item: Any) -> Dict[str, Any] | None:
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


def _extract_failure_reason(result: Dict[str, Any]) -> str:
    summary = dict((result or {}).get("summary") or {})
    reason = str(summary.get("reason") or "").strip()
    if reason:
        return reason
    error = str(summary.get("error") or "").strip()
    if error:
        return error
    return "agent_generation_failed"


def _call_solve_with_agent(**kwargs: Any) -> Dict[str, Any]:
    # 延迟导入，避免 facade <-> cross_runner 顶层循环依赖。
    from ..facade import solve_with_agent

    return dict(solve_with_agent(**kwargs) or {})


def _compare_codes_on_cases(
    *,
    code_a: str,
    code_b: str,
    test_cases: List[Dict[str, str]],
    can_brute_force: bool,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    trace_run_id: str = "",
) -> Dict[str, Any]:
    timeout_a = 3.0 if bool(can_brute_force) else 2.0
    timeout_b = 2.0

    cases: List[Dict[str, Any]] = []
    mismatch_cases: List[Dict[str, Any]] = []
    matched_cases = 0
    compared_cases = 0
    terminated_reason = ""

    for idx, case in enumerate(test_cases):
        stdin_text = str((case or {}).get("stdin") or "")
        out_a = _execute_code_once(code=code_a, stdin_text=stdin_text, timeout_sec=timeout_a)
        if bool(can_brute_force) and out_a.is_timeout:
            cases.append(
                {
                    "index": idx,
                    "stdin": stdin_text,
                    "out_a": "",
                    "out_b": "",
                    "match": True,
                    "skipped": True,
                    "skip_reason": "brute_timeout",
                    "error_a": out_a.error,
                    "error_b": "",
                }
            )
            continue
        if not bool(can_brute_force) and out_a.is_timeout:
            terminated_reason = "optimized_timeout"
            break
        if out_a.run_type in {RESULT_GRAMMATICAL_ERROR, RESULT_RUNTIME_ERROR, RESULT_SYSTEM_ERROR}:
            terminated_reason = "brute_execution_error" if bool(can_brute_force) else "optimized_execution_error"
            break

        out_b = _execute_code_once(code=code_b, stdin_text=stdin_text, timeout_sec=timeout_b)
        if out_b.is_timeout:
            terminated_reason = "optimized_timeout"
            break
        if out_b.run_type in {RESULT_GRAMMATICAL_ERROR, RESULT_RUNTIME_ERROR, RESULT_SYSTEM_ERROR}:
            terminated_reason = "optimized_execution_error"
            break

        left = str(out_a.output or "").rstrip()
        right = str(out_b.output or "").rstrip()
        is_match = left == right
        compared_cases += 1
        if is_match:
            matched_cases += 1
        one = {
            "index": idx,
            "stdin": stdin_text,
            "out_a": str(out_a.output or ""),
            "out_b": str(out_b.output or ""),
            "match": bool(is_match),
            "error_a": str(out_a.error or ""),
            "error_b": str(out_b.error or ""),
        }
        cases.append(one)
        if not is_match:
            mismatch_cases.append(one)
            break  # 遇错即停：发现第一个不匹配用例后立即终止扫描
        if on_progress and (compared_cases % 5 == 0 or compared_cases == len(test_cases)):
            _safe_report_progress(
                on_progress,
                {
                    "stage": "compare_progress",
                    "message": f"对拍: {compared_cases}/{len(test_cases)}, 已匹配 {matched_cases}",
                    "current": compared_cases,
                    "total": len(test_cases),
                    "matched": matched_cases,
                },
                trace_run_id=trace_run_id,
            )

    all_match = (
        not terminated_reason
        and compared_cases > 0
        and len(mismatch_cases) == 0
    )
    _safe_report_progress(
        on_progress,
        {
            "stage": "compare_done",
            "message": f"对拍完成: {compared_cases}/{len(test_cases)} 已比, {matched_cases} 匹配" + (f", 终止: {terminated_reason}" if terminated_reason else ""),
        },
        trace_run_id=trace_run_id,
    )
    return {
        "all_match": all_match,
        "total_cases": len(test_cases),
        "matched_cases": matched_cases,
        "compared_cases": compared_cases,
        "cases": cases,
        "mismatch_cases": mismatch_cases,
        "first_mismatch": mismatch_cases[0] if mismatch_cases else {},
        "terminated_reason": terminated_reason,
    }


def _execute_code_once(*, code: str, stdin_text: str, timeout_sec: float) -> _ExecOutcome:
    executor = build_default_run_executor(timeout_sec=float(timeout_sec or 2.0))
    state = AgentSessionState(
        session_id="verifier-exec",
        current_code=str(code or ""),
        runtime_context={
            "stdin": str(stdin_text or ""),
            "expected_stdout": None,
            "checker": None,
            "timeout_sec": float(timeout_sec or 2.0),
        },
    )
    result = dict(executor(str(code or ""), state) or {})
    run_type = str(result.get("type") or "")
    is_timeout = run_type == RESULT_RUNTIME_ERROR and "time" in result
    if run_type == RESULT_RIGHT:
        return _ExecOutcome(
            run_type=run_type,
            output=str(result.get("code") or ""),
            error="",
            is_timeout=is_timeout,
        )
    if run_type == RESULT_WRONG:
        return _ExecOutcome(
            run_type=run_type,
            output=str(result.get("result") or ""),
            error="",
            is_timeout=is_timeout,
        )
    return _ExecOutcome(
        run_type=run_type,
        output="",
        error=str(result.get("code") or result.get("result") or ""),
        is_timeout=is_timeout,
    )


def _retry_single_agent_after_mismatch(
    *,
    slot: str,
    algorithm: Dict[str, str],
    problem: Dict[str, Any],
    llm_caller_factory: Callable,
    current_code: str,
    used_rounds: int,
    max_rounds: int,
    feedback: Dict[str, str],
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    trace_run_id: str = "",
) -> Dict[str, Any]:
    extra_budget = min(5, max(0, int(max_rounds or 1) - int(used_rounds or 0)))
    if extra_budget <= 0:
        return {
            "success": False,
            "rounds": int(used_rounds or 0),
            "code": str(current_code or ""),
            "error": "retry_budget_exhausted",
        }

    session_tag = build_session_tag(slot=slot, algorithm_name=str(algorithm.get("name") or "algorithm"))
    llm_caller = llm_caller_factory(session_tag)
    if not callable(llm_caller):
        return {
            "success": False,
            "rounds": int(used_rounds or 0),
            "code": str(current_code or ""),
            "error": "llm_caller_factory_returned_non_callable",
        }

    problem_retry = dict(problem or {})
    problem_retry["cross_feedback"] = {
        "instruction": CROSS_FEEDBACK_INSTRUCTION,
        "stdin": str(feedback.get("stdin") or ""),
        "your_output": str(feedback.get(f"out_{slot}") or ""),
        "peer_output": str(feedback.get("out_b" if slot == "a" else "out_a") or ""),
    }

    runtime_context = {
        "problem": dict(problem_retry),
        "data_samples": _extract_data_samples(problem_retry),
        "agent_system_prompt": build_algorithm_system_prompt(
            algorithm_name=str(algorithm.get("name") or ""),
            algorithm_desc=str(algorithm.get("description") or ""),
        ),
        "disable_trace_store": True,
    }

    algo_name = str(algorithm.get("name") or "algorithm")
    _safe_report_progress(
        on_progress,
        {
            "stage": "retry_start",
            "message": f"Agent-{slot.upper()}[{algo_name}] 交叉修复中 (最多{extra_budget}轮)",
        },
        trace_run_id=trace_run_id,
    )

    def _on_retry_step(step_info: Dict[str, Any]) -> None:
        action = str((step_info or {}).get("action") or "unknown")
        attempt = int((step_info or {}).get("attempt") or 0)
        result_type = str((step_info or {}).get("type") or "")
        _safe_report_progress(
            on_progress,
            {
                "stage": "agent_step",
                "slot": slot,
                "algorithm": algo_name,
                "action": action,
                "attempt": attempt,
                "result_type": result_type,
                "message": f"Agent-{slot.upper()}[{algo_name}] 修复第{attempt}轮 {action}" + (f" → {result_type}" if result_type else ""),
                "details": {"payload": dict((step_info or {}).get("action_payload") or {})},
            },
            trace_run_id=trace_run_id,
        )

    result = _call_solve_with_agent(
        problem=problem_retry,
        candidate_code=str(current_code or ""),
        llm_caller=llm_caller,
        runtime_context=runtime_context,
        max_attempts=extra_budget,
        context_rounds=min(extra_budget, 30),
        session_id=session_tag,
        on_step_callback=_on_retry_step,
    )
    passed = bool((result or {}).get("passed"))
    used = int(used_rounds or 0) + int((result or {}).get("attempts") or 0)
    new_code = str((result or {}).get("final_code") or "")
    if passed and new_code:
        return {"success": True, "rounds": used, "code": new_code, "error": ""}
    return {
        "success": False,
        "rounds": used,
        "code": str(new_code or current_code or ""),
        "error": _extract_failure_reason(result),
    }


def _build_verdict(
    *,
    passed: bool,
    strategy: str = "",
    analysis: Dict[str, Any],
    reason: str = "",
    total_cases: int,
    matched_cases: int,
    cases: tuple[Dict[str, Any], ...],
    mismatch_cases: tuple[Dict[str, Any], ...] = (),
    error: str = "",
    code_a: str = "",
    code_b: str = "",
    a_success: bool = False,
    b_success: bool = False,
    a_rounds: int = 0,
    b_rounds: int = 0,
) -> Dict[str, Any]:
    if passed:
        strategy = str(strategy or VERIFY_STRATEGY_CROSS)
        reason = "outputs_match"
        mismatch_cases = ()
        error = ""
    else:
        strategy = str(strategy or VERIFY_STRATEGY_SKIP)
        reason = str(reason or "verification_degraded")

    verdict = {
        "strategy": strategy,
        "passed": passed,
        "verification_passed": passed,
        "reason": reason,
        "total_cases": int(total_cases or 0),
        "matched_cases": int(matched_cases or 0),
        "cases": list(cases),
        "mismatch_cases": list(mismatch_cases),
        "analysis": dict(analysis or {}),
        "skip_reason": str(reason or ""),
        "error": str(error or ""),
        "code_a": str(code_a or ""),
        "code_b": str(code_b or ""),
        "a_success": bool(a_success),
        "b_success": bool(b_success),
        "a_rounds": int(a_rounds or 0),
        "b_rounds": int(b_rounds or 0),
    }
    return verdict
