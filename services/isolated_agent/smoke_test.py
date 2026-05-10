"""Isolated Agent 冒烟测试脚本。

覆盖目标：
- 验证单入口 facade 能完成最小闭环（modify -> run -> self_test -> finalize）。
- 验证最大尝试次数参数生效，避免死循环。

运行方式：
- 建议在项目根目录执行：
  `python -m 数据生成器.services.isolated_agent.smoke_test`
"""

from __future__ import annotations

from typing import Any, Dict, List

from 数据生成器.services.isolated_agent import solve_with_agent

_FIXED_CODE = """def solve(problem: dict, data: dict) -> dict:
    stdin_text = str(data.get("stdin") or "").strip()
    return {"stdout": stdin_text + "\\n", "meta": {}}
"""

_BAD_CODE = """def solve(problem: dict, data: dict) -> dict:
    return {"stdout": "BAD\\n", "meta": {}}
"""


def _make_success_llm() -> Any:
    state = {"turn": 0}

    def _caller(messages: List[Dict[str, str]]) -> Dict[str, Any]:
        _ = messages
        state["turn"] += 1
        t = int(state["turn"])
        if t == 1:
            return {"action": "read", "target": "all"}
        if t == 2:
            return {"action": "modify", "code": _FIXED_CODE}
        if t == 3:
            return {"action": "run", "code": _FIXED_CODE}
        if t == 4:
            return {
                "action": "self_test",
                "cases": [
                    {"label": "empty", "stdin": "", "timeout_sec": 1.5},
                    {"label": "normal", "stdin": "HELLO\n", "timeout_sec": 1.5},
                ],
            }
        return {"action": "finalize"}

    return _caller


def _make_stuck_llm() -> Any:
    def _caller(messages: List[Dict[str, str]]) -> Dict[str, Any]:
        _ = messages
        return {"action": "read"}

    return _caller


def _assert_success_case() -> Dict[str, Any]:
    problem = {"title": "echo", "description": "输出输入内容", "constraints": "任意单行字符串"}
    runtime_context = {"stdin": "HELLO", "expected_stdout": "HELLO\n"}
    result = solve_with_agent(
        problem=problem,
        candidate_code=_BAD_CODE,
        llm_caller=_make_success_llm(),
        runtime_context=runtime_context,
        max_attempts=15,
        context_rounds=3,
        session_id="smoke-success",
    )
    assert bool(result.get("passed")) is True, f"success case failed: {result}"
    assert "stdin_text" in str(result.get("final_code") or ""), "final code not updated to fixed code"
    return result


def _assert_attempt_limit_case() -> Dict[str, Any]:
    problem = {"title": "echo", "description": "输出输入内容", "constraints": "任意单行字符串"}
    runtime_context = {"stdin": "HELLO", "expected_stdout": "HELLO\n"}
    result = solve_with_agent(
        problem=problem,
        candidate_code=_BAD_CODE,
        llm_caller=_make_stuck_llm(),
        runtime_context=runtime_context,
        max_attempts=4,
        context_rounds=3,
        session_id="smoke-limit",
    )
    assert bool(result.get("passed")) is False, "attempt limit case should fail"
    attempts = int(result.get("attempts") or 0)
    assert attempts <= 4, f"attempt limit not respected: {attempts}"
    return result


def main() -> None:
    ok_result = _assert_success_case()
    limit_result = _assert_attempt_limit_case()
    print("[smoke] success case passed.")
    print(f"[smoke] success attempts={ok_result.get('attempts')} session={ok_result.get('session_id')}")
    print("[smoke] attempt-limit case passed.")
    print(f"[smoke] limit attempts={limit_result.get('attempts')} session={limit_result.get('session_id')}")
    print("[smoke] all checks passed.")


if __name__ == "__main__":
    main()
