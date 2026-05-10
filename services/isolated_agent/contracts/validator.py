"""Isolated Agent 请求校验器。

核心职责：
- 对单条动作请求做严格结构校验，确保 dispatcher 只处理可信输入。
- 输出标准化后的动作负载，减少 controller 分支中的防御性代码。

不负责：
- 不操作会话状态。
- 不执行 run/modify/finalize/self_test 业务逻辑。

数据流：
- 外部 payload -> `validate_action_request` -> 标准化 dict -> dispatcher handler。

约束：
- 每轮只允许一个动作对象。
- `run/modify` 必须携带完整 `code` 文本。
- `finalize` 可选携带 `code_hash` 用于提交一致性校验。
"""

from __future__ import annotations

from typing import Any, Dict

from .errors import (
    ERR_INVALID_ACTION,
    ERR_INVALID_FIELD_TYPE,
    ERR_INVALID_PAYLOAD,
    ERR_MISSING_FIELD,
    ProtocolViolation,
)
from .protocol import (
    ALLOWED_ACTIONS,
    ALLOWED_READ_TARGETS,
    READ_TARGET_CODE,
    RUN_MODE_CURRENT,
    RUN_MODE_INLINE,
    SELF_TEST_DEFAULT_TIMEOUT_SEC,
    SELF_TEST_MAX_CASES,
    SELF_TEST_MIN_CASES,
    SELF_TEST_TIMEOUT_MAX_SEC,
    SELF_TEST_TIMEOUT_MIN_SEC,
)


def validate_action_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验并标准化动作请求。

    为什么在入口强校验：
    - 避免 controller 在业务路径里重复处理结构异常。
    - 统一把“格式错误”与“业务失败”区分开，便于外层做稳定重试策略。
    """

    if not isinstance(payload, dict):
        raise ProtocolViolation(
            error_code=ERR_INVALID_PAYLOAD,
            message="action payload must be a JSON object",
        )

    action = str(payload.get("action") or "").strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise ProtocolViolation(
            error_code=ERR_INVALID_ACTION,
            message="unsupported action",
            details={"action": action, "allowed": list(ALLOWED_ACTIONS)},
        )

    normalized: Dict[str, Any] = {"action": action}
    if action == "read":
        target = payload.get("target", READ_TARGET_CODE)
        if not isinstance(target, str):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'target' must be a string when provided",
                details={"action": action, "field": "target"},
            )
        target_text = str(target or READ_TARGET_CODE).strip().lower() or READ_TARGET_CODE
        if target_text not in ALLOWED_READ_TARGETS:
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'target' is unsupported",
                details={"action": action, "field": "target", "allowed": list(ALLOWED_READ_TARGETS)},
            )
        normalized["target"] = target_text
        return normalized

    if action == "modify":
        code = payload.get("code")
        if code is None:
            raise ProtocolViolation(
                error_code=ERR_MISSING_FIELD,
                message="'code' is required for modify",
                details={"action": action},
            )
        if not isinstance(code, str):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'code' must be a string",
                details={"action": action, "field": "code"},
            )
        normalized["code"] = code
        return normalized

    if action == "run":
        code = payload.get("code")
        use_current = bool(payload.get("use_current", False))
        if code is None and not use_current:
            raise ProtocolViolation(
                error_code=ERR_MISSING_FIELD,
                message="'run' requires either 'code' (str) or 'use_current' (true)",
                details={"action": action},
            )
        if code is not None and not isinstance(code, str):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'code' must be a string",
                details={"action": action, "field": "code"},
            )
        if use_current:
            normalized["mode"] = RUN_MODE_CURRENT
        else:
            normalized["mode"] = RUN_MODE_INLINE
            normalized["code"] = code
        return normalized

    if action == "finalize":
        code_hash = payload.get("code_hash")
        if code_hash is not None and not isinstance(code_hash, str):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'code_hash' must be a string when provided",
                details={"action": action, "field": "code_hash"},
            )
        if isinstance(code_hash, str):
            normalized["code_hash"] = code_hash.strip()
        return normalized

    if action == "self_test":
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'cases' must be a list for self_test",
                details={"action": action, "field": "cases"},
            )
        if not (SELF_TEST_MIN_CASES <= len(cases) <= SELF_TEST_MAX_CASES):
            raise ProtocolViolation(
                error_code=ERR_INVALID_FIELD_TYPE,
                message="'cases' length is out of range",
                details={
                    "action": action,
                    "field": "cases",
                    "min": SELF_TEST_MIN_CASES,
                    "max": SELF_TEST_MAX_CASES,
                },
            )

        normalized_cases = []
        for idx, item in enumerate(cases):
            if not isinstance(item, dict):
                raise ProtocolViolation(
                    error_code=ERR_INVALID_FIELD_TYPE,
                    message="each self_test case must be an object",
                    details={"action": action, "field": f"cases[{idx}]"},
                )
            stdin = item.get("stdin")
            if stdin is None or not isinstance(stdin, str):
                raise ProtocolViolation(
                    error_code=ERR_MISSING_FIELD,
                    message="each self_test case requires string 'stdin'",
                    details={"action": action, "field": f"cases[{idx}].stdin"},
                )

            timeout_val = item.get("timeout_sec", SELF_TEST_DEFAULT_TIMEOUT_SEC)
            if not isinstance(timeout_val, (int, float)):
                raise ProtocolViolation(
                    error_code=ERR_INVALID_FIELD_TYPE,
                    message="'timeout_sec' must be a number when provided",
                    details={"action": action, "field": f"cases[{idx}].timeout_sec"},
                )
            timeout_sec = float(timeout_val)
            if not (SELF_TEST_TIMEOUT_MIN_SEC <= timeout_sec <= SELF_TEST_TIMEOUT_MAX_SEC):
                raise ProtocolViolation(
                    error_code=ERR_INVALID_FIELD_TYPE,
                    message="'timeout_sec' is out of allowed range",
                    details={
                        "action": action,
                        "field": f"cases[{idx}].timeout_sec",
                        "min": SELF_TEST_TIMEOUT_MIN_SEC,
                        "max": SELF_TEST_TIMEOUT_MAX_SEC,
                    },
                )

            normalized_case: Dict[str, Any] = {"stdin": stdin, "timeout_sec": timeout_sec}
            label = item.get("label")
            if label is not None:
                if not isinstance(label, str):
                    raise ProtocolViolation(
                        error_code=ERR_INVALID_FIELD_TYPE,
                        message="'label' must be a string when provided",
                        details={"action": action, "field": f"cases[{idx}].label"},
                    )
                normalized_case["label"] = label.strip()
            normalized_cases.append(normalized_case)

        normalized["cases"] = normalized_cases
        return normalized

    return normalized
