"""Isolated Agent 动作分发入口。

核心职责：
- 接收单动作 payload，统一做协议校验后分发到 read/run/modify/self_test/finalize。
- 对会话状态做最小且可预测的更新（版本号、摘要、提交状态）。
- 将校验错误与业务状态冲突统一序列化为稳定响应结构。

不负责：
- 不实现具体沙箱运行逻辑（由后续 runtime 层注入 `run_executor`）。
- 不拼装长上下文提示词（仅维护 recent_events 摘要）。

数据流：
- 外部 action payload -> validator -> handler -> session state update -> JSON response。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from ..contracts.errors import (
    ERR_CODE_HASH_MISMATCH,
    ERR_DATA_SAMPLE_MISSING,
    ERR_DATA_SAMPLE_TOO_COMPLEX,
    ERR_FINALIZE_REQUIRES_PASS,
    ERR_RUN_EXECUTOR_MISSING,
    ERR_RUN_RESULT_INVALID,
    ERR_VERSION_NOT_FOUND,
    ProtocolViolation,
)
from ..contracts.protocol import (
    ACTION_FINALIZE,
    ACTION_MODIFY,
    ACTION_READ,
    ACTION_RUN,
    ACTION_SELF_TEST,
    ALLOWED_READ_TARGETS,
    DATA_SAMPLE_MAX_CHARS,
    READ_TARGET_ALL,
    READ_TARGET_CODE,
    READ_TARGET_DATA_SAMPLE,
    READ_TARGET_PROBLEM,
    READ_TARGET_VERSION_PREFIX,
    RESULT_FINALIZE_OK,
    RESULT_MODIFY_OK,
    RESULT_PROTOCOL_ERROR,
    RESULT_READ_OK,
    RESULT_SELF_TEST_OK,
    RUN_MODE_CURRENT,
    RUN_RESULT_TYPES,
    SELF_TEST_STATUS_OK,
    SELF_TEST_STATUS_RUNTIME_ERROR,
    SELF_TEST_STATUS_TIMEOUT,
)
from ..contracts.validator import validate_action_request
from ..runtime.executor import run_self_test_cases
from ..session.state import AgentSessionState

RunExecutor = Callable[[str, AgentSessionState], Dict[str, Any]]


class ActionDispatcher:
    def __init__(self, *, run_executor: Optional[RunExecutor] = None):
        self._run_executor = run_executor

    def dispatch(self, payload: Dict[str, Any], state: AgentSessionState) -> Dict[str, Any]:
        try:
            req = validate_action_request(payload)
            action = str(req["action"])
            if action == ACTION_READ:
                return self._handle_read(state, req)
            if action == ACTION_MODIFY:
                return self._handle_modify(state, req)
            if action == ACTION_RUN:
                return self._handle_run(state, req)
            if action == ACTION_SELF_TEST:
                return self._handle_self_test(state, req)
            if action == ACTION_FINALIZE:
                return self._handle_finalize(state, req)
            raise ProtocolViolation(error_code="invalid_action", message="unsupported action")
        except ProtocolViolation as e:
            if isinstance(state, AgentSessionState):
                state.push_event(
                    action=str(payload.get("action") or ""),
                    result_type=RESULT_PROTOCOL_ERROR,
                    note=e.error_code,
                    extra={
                        "protocol_message": str(e.message or ""),
                        "protocol_details": dict(e.details or {}),
                    },
                )
            return e.to_payload()

    def _handle_read(self, state: AgentSessionState, req: Dict[str, Any]) -> Dict[str, Any]:
        target = str(req.get("target") or READ_TARGET_CODE).strip().lower() or READ_TARGET_CODE
        if target not in ALLOWED_READ_TARGETS:
            if target.startswith(READ_TARGET_VERSION_PREFIX):
                return self._handle_read_version(state, target)
            target = READ_TARGET_CODE

        problem_payload = state.runtime_context.get("problem")
        problem_text = str(problem_payload if problem_payload is not None else {})
        code_text = state.current_code
        if target == READ_TARGET_PROBLEM:
            file_block = "[start_problem]\n" + problem_text + "\n[end_problem]"
        elif target == READ_TARGET_DATA_SAMPLE:
            data_sample_text = self._resolve_data_sample_text(state)
            file_block = "[start_data_sample]\n" + data_sample_text + "\n[end_data_sample]"
        elif target == READ_TARGET_ALL:
            file_block = (
                "[start_problem]\n"
                + problem_text
                + "\n[end_problem]\n"
                + "[start_file]\n"
                + code_text
                + "\n[start_end]"
            )
        else:
            file_block = "[start_file]\n" + code_text + "\n[start_end]"

        state.push_event(
            action=ACTION_READ,
            result_type=RESULT_READ_OK,
            note=f"read:{target}",
            extra={"read_file_preview": self._clip_for_context(file_block, limit=6000)},
        )
        return {
            "type": RESULT_READ_OK,
            "target": target,
            "version": int(state.version),
            "code_hash": str(state.code_hash),
            "file": file_block,
        }

    def _resolve_data_sample_text(self, state: AgentSessionState) -> str:
        samples = self._collect_data_samples(state)
        if not samples:
            raise ProtocolViolation(
                error_code=ERR_DATA_SAMPLE_MISSING,
                message="data_sample is not available in current runtime context",
            )
        serialized = [self._serialize_sample(item) for item in samples]
        shortest = min(serialized, key=len)
        if len(shortest) > int(DATA_SAMPLE_MAX_CHARS):
            raise ProtocolViolation(
                error_code=ERR_DATA_SAMPLE_TOO_COMPLEX,
                message="data sample is too complex; please provide a manual simplified sample",
                details={
                    "min_chars": len(shortest),
                    "limit_chars": int(DATA_SAMPLE_MAX_CHARS),
                },
            )
        return shortest

    def _collect_data_samples(self, state: AgentSessionState) -> list[Any]:
        runtime = dict(state.runtime_context or {})
        data_samples = runtime.get("data_samples")
        if isinstance(data_samples, list):
            return [item for item in data_samples if item is not None]
        sample_one = runtime.get("data_sample")
        if sample_one is not None:
            return [sample_one]
        problem_obj = runtime.get("problem")
        if isinstance(problem_obj, dict):
            problem_samples = problem_obj.get("data_samples")
            if isinstance(problem_samples, list):
                return [item for item in problem_samples if item is not None]
            if any(k in problem_obj for k in ("stdin", "vars", "meta", "stdout")):
                return [
                    {
                        "stdin": problem_obj.get("stdin", ""),
                        "stdout": problem_obj.get("stdout", ""),
                        "vars": problem_obj.get("vars", {}),
                        "meta": problem_obj.get("meta", {}),
                    }
                ]
        return []

    def _serialize_sample(self, sample: Any) -> str:
        if isinstance(sample, (dict, list)):
            try:
                return json.dumps(sample, ensure_ascii=False, indent=2)
            except Exception:
                return str(sample)
        return str(sample)

    def _handle_modify(self, state: AgentSessionState, req: Dict[str, Any]) -> Dict[str, Any]:
        new_code = str(req.get("code") or "")
        summary = str(req.get("summary") or "").strip() or "code modified"
        old_code = state.current_code
        old_version = int(state.version)
        state.push_snapshot(old_version, old_code, summary)
        state.replace_code(new_code)
        state.push_event(action=ACTION_MODIFY, result_type=RESULT_MODIFY_OK, note=summary)
        return {
            "type": RESULT_MODIFY_OK,
            "version": int(state.version),
            "code_hash": str(state.code_hash),
        }

    def _handle_run(self, state: AgentSessionState, req: Dict[str, Any]) -> Dict[str, Any]:
        if self._run_executor is None:
            raise ProtocolViolation(
                error_code=ERR_RUN_EXECUTOR_MISSING,
                message="run executor is not configured",
            )
        # use_current 模式：直接使用 state.current_code，不需要 LLM 重传完整代码
        if str(req.get("mode") or "") == RUN_MODE_CURRENT:
            run_code = state.current_code
        else:
            run_code = str(req.get("code") or "")
        result = self._run_executor(run_code, state)
        result_type = str((result or {}).get("type") or "").strip()
        if result_type not in RUN_RESULT_TYPES:
            raise ProtocolViolation(
                error_code=ERR_RUN_RESULT_INVALID,
                message="run executor returned unsupported result type",
                details={"type": result_type, "allowed": list(RUN_RESULT_TYPES)},
            )

        if result_type == "right":
            # 通过后将“已验证代码”升格为权威当前代码，避免 finalize 提交到旧版本。
            state.replace_code(run_code)
        state.mark_run_result(result_type)
        run_err = ""
        if result_type != "right":
            raw_output = str((result or {}).get("result") or (result or {}).get("code") or "")
            run_err = self._clip_for_context(raw_output, limit=300)
        state.push_event(
            action=ACTION_RUN,
            result_type=result_type,
            note="run completed",
            extra={"run_err": run_err} if run_err else {},
        )

        out = dict(result or {})
        out["version"] = int(state.version)
        out["code_hash"] = str(state.code_hash)
        return out

    def _handle_read_version(self, state: AgentSessionState, target: str) -> Dict[str, Any]:
        ver = int(target[1:])
        snap = state.find_snapshot(ver)
        if snap is None:
            raise ProtocolViolation(
                error_code=ERR_VERSION_NOT_FOUND,
                message=f"version {target} not found (may have been evicted), check available_versions in context",
                details={
                    "target": target,
                    "available_versions": [s["version"] for s in state.version_snapshots],
                },
            )
        file_block = (
            f"[start_file_{target}]\n"
            f"version={snap['version']}\n"
            f"summary={snap['summary']}\n"
            f"code_hash={snap['code_hash']}\n"
            + str(snap["code"])
            + f"\n[end_file_{target}]"
        )
        state.push_event(
            action=ACTION_READ,
            result_type=RESULT_READ_OK,
            note=f"read:{target}",
            extra={"read_file_preview": self._clip_for_context(file_block, limit=6000)},
        )
        return {
            "type": RESULT_READ_OK,
            "target": target,
            "version": int(state.version),
            "code_hash": str(state.code_hash),
            "file": file_block,
        }

    def _clip_for_context(self, text: str, limit: int = 1200) -> str:
        s = str(text or "")
        lim = max(200, int(limit or 1200))
        if len(s) <= lim:
            return s
        head = int(lim * 0.7)
        tail = lim - head
        return s[:head] + "\n...\n(truncated)\n...\n" + s[-tail:]

    def _handle_finalize(self, state: AgentSessionState, req: Dict[str, Any]) -> Dict[str, Any]:
        required_hash = str(req.get("code_hash") or "").strip()
        if required_hash and required_hash != str(state.code_hash):
            raise ProtocolViolation(
                error_code=ERR_CODE_HASH_MISMATCH,
                message="finalize code hash mismatch",
                details={"expected": required_hash, "actual": state.code_hash},
            )
        can_finalize, reject_message = state.can_finalize()
        if not can_finalize:
            raise ProtocolViolation(
                error_code=ERR_FINALIZE_REQUIRES_PASS,
                message=reject_message,
                details={
                    "version": state.version,
                    "last_passed_version": state.last_passed_version,
                    "last_self_test_version": state.last_self_test_version,
                    "last_self_test_all_stable": state.last_self_test_all_stable,
                },
            )

        state.status = "finished"
        state.push_event(action=ACTION_FINALIZE, result_type=RESULT_FINALIZE_OK, note="finalized")
        return {
            "type": RESULT_FINALIZE_OK,
            "session_id": state.session_id,
            "version": int(state.version),
            "code_hash": str(state.code_hash),
            "final_code": state.current_code,
            "summary": {
                "status": "ok",
                "last_run_result_type": state.last_run_result_type,
                "recent_events": list(state.recent_events),
            },
        }

    def _handle_self_test(self, state: AgentSessionState, req: Dict[str, Any]) -> Dict[str, Any]:
        if self._run_executor is None:
            raise ProtocolViolation(
                error_code=ERR_RUN_EXECUTOR_MISSING,
                message="run executor is not configured",
            )

        cases = list(req.get("cases") or [])
        result = run_self_test_cases(
            run_executor=self._run_executor,
            state=state,
            code=state.current_code,
            cases=cases,
        )
        all_stable = bool((result or {}).get("all_stable"))
        state.mark_self_test_result(all_stable=all_stable)

        items = list((result or {}).get("results") or [])
        ok_count = sum(1 for x in items if str((x or {}).get("status") or "") == SELF_TEST_STATUS_OK)
        timeout_count = sum(1 for x in items if str((x or {}).get("status") or "") == SELF_TEST_STATUS_TIMEOUT)
        crash_count = sum(1 for x in items if str((x or {}).get("status") or "") == SELF_TEST_STATUS_RUNTIME_ERROR)

        state.push_event(
            action=ACTION_SELF_TEST,
            result_type=RESULT_SELF_TEST_OK,
            note=f"self_test stable={all_stable}",
            extra={
                "self_test_total": len(items),
                "self_test_ok": ok_count,
                "self_test_timeout": timeout_count,
                "self_test_runtime_error": crash_count,
                "self_test_all_stable": all_stable,
            },
        )
        return {
            "type": RESULT_SELF_TEST_OK,
            "all_stable": all_stable,
            "results": items,
            "version": int(state.version),
            "code_hash": str(state.code_hash),
        }
