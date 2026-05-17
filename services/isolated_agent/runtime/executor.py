"""Isolated Agent 受限执行器（阶段 4）。

核心职责：
- 将候选代码放入隔离子进程执行，并统一映射为协议约定的 run 结果类型。
- 在执行前应用 `guard` 做静态安全拦截，避免明显越权代码进入运行阶段。
- 提供默认校验策略：无期望时按“可运行”判 right；有期望时做 stdout 比对。

不负责：
- 不维护会话生命周期。
- 不拼装 AI 对话上下文。

关键协作关系：
- `controller.dispatcher` 调用本模块提供的 `build_default_run_executor`。
- `session.state.runtime_context` 提供运行输入与可选校验器（checker）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Callable, Dict, Optional

from ..contracts.protocol import (
    ALLOWED_SELF_TEST_STATUSES,
    RESULT_GRAMMATICAL_ERROR,
    RESULT_RIGHT,
    RESULT_RUNTIME_ERROR,
    RESULT_SELF_TEST_OK,
    RESULT_SYSTEM_ERROR,
    RESULT_WRONG,
    SELF_TEST_DEFAULT_TIMEOUT_SEC,
    SELF_TEST_STATUS_OK,
    SELF_TEST_STATUS_RUNTIME_ERROR,
    SELF_TEST_STATUS_TIMEOUT,
)
from ..session.state import AgentSessionState
from .guard import check_code_safety

CheckerFunc = Callable[[str, Dict[str, Any]], bool]

_DEFAULT_TIMEOUT_SEC = 3.0
_DEFAULT_MAX_OUTPUT_CHARS = 12000


def _clip(text: str, limit: int) -> str:
    s = str(text or "")
    lim = max(256, int(limit or _DEFAULT_MAX_OUTPUT_CHARS))
    if len(s) <= lim:
        return s
    head = int(lim * 0.7)
    tail = lim - head
    return s[:head] + "\n...\n(truncated)\n...\n" + s[-tail:]


def _build_runner_script(candidate_code: str) -> str:
    return "\n".join(
        [
            candidate_code.rstrip(),
            "",
            "import json",
            "import os",
            "import traceback",
            "",
            "def __isolated_entry__():",
            "    try:",
            "        fn = globals().get('solve')",
            "        if not callable(fn):",
            "            raise RuntimeError('solve_not_found')",
            "        raw = os.environ.get('ISOLATED_AGENT_CASE') or '{}'",
            "        case = json.loads(raw)",
            "        stdin_text = str(case.get('stdin') or '')",
            "        data = {'stdin': stdin_text, 'stdout': '', 'meta': {}, 'vars': {}}",
            "        result = fn({}, data)",
            "        if isinstance(result, dict):",
            "            out = result.get('stdout', '')",
            "        elif result is None:",
            "            out = ''",
            "        else:",
            "            out = str(result)",
            "        print('' if out is None else str(out), end='')",
            "        return 0",
            "    except Exception:",
            "        traceback.print_exc()",
            "        return 2",
            "",
            "raise SystemExit(__isolated_entry__())",
        ]
    )


def _default_checker(stdout_text: str, context: Dict[str, Any]) -> bool:
    expected = context.get("expected_stdout")
    if expected is None:
        return True
    left = str(stdout_text or "").strip()
    right = str(expected or "").strip()
    return left == right


def build_default_run_executor(
    *,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
) -> Callable[[str, AgentSessionState], Dict[str, Any]]:
    """构造默认 run 执行器。

    使用工厂函数而不是全局单例：
    - 便于不同会话按策略注入不同超时/输出限制。
    - 避免并行会话共享可变状态。
    """

    def _run_executor(code: str, state: AgentSessionState) -> Dict[str, Any]:
        code_text = str(code or "")
        guard_hit = check_code_safety(code_text)
        if guard_hit is not None:
            return {
                "type": RESULT_SYSTEM_ERROR,
                "code": f"{guard_hit.rule}:{guard_hit.detail}",
            }
        if guard_hit is None:
            compile_err = _check_syntax(code_text)
            if compile_err:
                return {"type": RESULT_GRAMMATICAL_ERROR, "code": compile_err}

        run_ctx = dict(getattr(state, "runtime_context", {}) or {})
        stdin_text = str(run_ctx.get("stdin") or "")
        out_limit = int(run_ctx.get("max_output_chars") or max_output_chars)
        timeout_val = float(run_ctx.get("timeout_sec") or timeout_sec)
        checker = run_ctx.get("checker")
        if checker is not None and not callable(checker):
            checker = None

        payload = _execute_subprocess(code_text, stdin_text=stdin_text, timeout_sec=timeout_val, max_output_chars=out_limit)
        if payload["type"] in {RESULT_RUNTIME_ERROR, RESULT_GRAMMATICAL_ERROR, RESULT_SYSTEM_ERROR}:
            return payload

        stdout_text = str(payload.get("code") or "")
        checker_func: CheckerFunc = checker if callable(checker) else _default_checker
        ok = False
        try:
            ok = bool(checker_func(stdout_text, run_ctx))
        except Exception:
            ok = False
        if ok:
            return {
                "type": RESULT_RIGHT,
                "code": stdout_text,
            }
        return {
            "type": RESULT_WRONG,
            "result": stdout_text,
        }

    return _run_executor


def run_self_test_cases(
    *,
    run_executor: Callable[[str, AgentSessionState], Dict[str, Any]],
    state: AgentSessionState,
    code: str,
    cases: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """执行稳定性自测。

    设计原因：
    - 自测目标仅是“能否稳定运行”，不具备正确性 oracle，因此不做 expected 输出比对。
    - 通过临时覆盖 runtime_context 的 stdin/timeout 复用现有受限执行器，避免旁路执行链。
    """

    original_ctx = dict(getattr(state, "runtime_context", {}) or {})
    results: list[Dict[str, Any]] = []
    all_stable = True
    try:
        for idx, item in enumerate(cases):
            stdin_text = str((item or {}).get("stdin") or "")
            timeout_sec = float((item or {}).get("timeout_sec") or SELF_TEST_DEFAULT_TIMEOUT_SEC)
            label = str((item or {}).get("label") or "").strip()
            case_ctx = dict(original_ctx)
            case_ctx["stdin"] = stdin_text
            case_ctx["expected_stdout"] = None
            case_ctx["checker"] = None
            case_ctx["timeout_sec"] = timeout_sec
            state.runtime_context = case_ctx

            run_out = run_executor(code, state)
            run_type = str((run_out or {}).get("type") or "").strip()
            status = _map_self_test_status(run_type, run_out)
            if status != SELF_TEST_STATUS_OK:
                all_stable = False

            one: Dict[str, Any] = {"index": idx, "status": status}
            if label:
                one["label"] = label
            results.append(one)
    finally:
        state.runtime_context = original_ctx

    return {
        "type": RESULT_SELF_TEST_OK,
        "all_stable": bool(all_stable),
        "results": results,
    }


def _map_self_test_status(run_type: str, run_result: Dict[str, Any]) -> str:
    if run_type in {RESULT_RIGHT, RESULT_WRONG}:
        status = SELF_TEST_STATUS_OK
    elif run_type == RESULT_RUNTIME_ERROR and "time" in (run_result or {}):
        status = SELF_TEST_STATUS_TIMEOUT
    else:
        status = SELF_TEST_STATUS_RUNTIME_ERROR
    if status not in ALLOWED_SELF_TEST_STATUSES:
        return SELF_TEST_STATUS_RUNTIME_ERROR
    return status


def _check_syntax(code_text: str) -> str:
    try:
        compile(code_text, "<isolated_agent>", "exec")
        return ""
    except Exception:
        return _clip(traceback.format_exc(), _DEFAULT_MAX_OUTPUT_CHARS)


def _execute_subprocess(
    code_text: str,
    *,
    stdin_text: str,
    timeout_sec: float,
    max_output_chars: int,
) -> Dict[str, Any]:
    script_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", delete=False) as f:
            script_path = f.name
            f.write(_build_runner_script(code_text))

        env = dict(os.environ)
        env["ISOLATED_AGENT_CASE"] = json.dumps({"stdin": stdin_text}, ensure_ascii=True)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--exec-script", script_path]
        else:
            cmd = [sys.executable, "-I", script_path]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.2, float(timeout_sec or _DEFAULT_TIMEOUT_SEC)),
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        stdout_text = _clip(proc.stdout or "", max_output_chars)
        stderr_text = _clip(proc.stderr or "", max_output_chars)

        if int(proc.returncode) == 0:
            return {"type": RESULT_RIGHT, "code": stdout_text}
        if "SyntaxError" in stderr_text:
            return {"type": RESULT_GRAMMATICAL_ERROR, "code": stderr_text}
        return {"type": RESULT_RUNTIME_ERROR, "code": stderr_text}
    except subprocess.TimeoutExpired as e:
        return {"type": RESULT_RUNTIME_ERROR, "time": max(1, int(timeout_sec or _DEFAULT_TIMEOUT_SEC)), "code": _clip(str(e), max_output_chars)}
    except Exception:
        return {"type": RESULT_SYSTEM_ERROR, "code": _clip(traceback.format_exc(), max_output_chars)}
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:
                pass
