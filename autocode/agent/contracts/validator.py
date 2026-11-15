"""autoCode 请求校验器（注册表驱动，设计 §4「校验」一行）。

核心职责：
- 对单条动作请求做严格结构校验，确保 dispatcher 只处理可信输入。
- 输出标准化后的动作负载，减少 controller 分支中的防御性代码。

不负责：
- 不操作会话状态。
- 不执行 run/modify/finalize/self_test 业务逻辑。

数据流：
- 外部 payload -> `validate_action_request` -> 查 `_VALIDATORS[action]` -> 标准化 dict -> dispatcher handler。

约束：
- 每轮只允许一个动作对象。
- `run/modify` 必须携带完整 `code` 文本。
- `finalize` 可选携带 `code_hash` 用于提交一致性校验。

为什么从 if 链改成注册表（旧实现的真实缺陷）：
- 旧代码末尾 `return normalized` 是一条 fall-through：当 `ALLOWED_ACTIONS` 扩了新动作
  （设计 §4 要加 run_case/compile/gen_data/cross_check/search/fetch）却忘了加 if 分支时，
  新动作会**零校验静默通过**，直抵 handler。
- 现在：未登记动作 -> `invalid_action`；已登记但缺 schema -> `validator_missing`。
  两条路径都 raise ProtocolViolation，禁止静默放行。
- 行为兼容性：既有 5 个动作的校验规则与错误码/错误详情逐条保持不变
  （由 tests/test_validator_registry.py 守住注册覆盖，由 kernel 行为测试守住语义）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Final

from .errors import (
    ERR_INVALID_ACTION,
    ERR_INVALID_FIELD_TYPE,
    ERR_INVALID_PAYLOAD,
    ERR_MISSING_FIELD,
    ERR_VALIDATOR_MISSING,
    ProtocolViolation,
)
from .protocol import (
    ACTION_FINALIZE,
    ACTION_MODIFY,
    ACTION_READ,
    ACTION_RUN,
    ACTION_SELF_TEST,
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

_VERSION_TARGET_RE = re.compile(r"^v([1-9]\d*)$")

# 单个动作的 schema 校验函数：payload -> 标准化字段（不含 action 本身）
_ActionValidator = Callable[[Dict[str, Any]], Dict[str, Any]]


def _is_version_target(target: str) -> bool:
    return bool(_VERSION_TARGET_RE.match(str(target or "").strip().lower()))


def _validate_read(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = ACTION_READ
    target = payload.get("target", READ_TARGET_CODE)
    if not isinstance(target, str):
        raise ProtocolViolation(
            error_code=ERR_INVALID_FIELD_TYPE,
            message="'target' must be a string when provided",
            details={"action": action, "field": "target"},
        )
    target_text = str(target or READ_TARGET_CODE).strip().lower() or READ_TARGET_CODE
    if target_text not in ALLOWED_READ_TARGETS and not _is_version_target(target_text):
        raise ProtocolViolation(
            error_code=ERR_INVALID_FIELD_TYPE,
            message="'target' is unsupported",
            details={"action": action, "field": "target", "allowed": list(ALLOWED_READ_TARGETS)},
        )
    return {"target": target_text}


def _validate_modify(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = ACTION_MODIFY
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
    return {"code": code}


def _validate_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = ACTION_RUN
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
        return {"mode": RUN_MODE_CURRENT}
    return {"mode": RUN_MODE_INLINE, "code": code}


def _validate_finalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = ACTION_FINALIZE
    code_hash = payload.get("code_hash")
    if code_hash is not None and not isinstance(code_hash, str):
        raise ProtocolViolation(
            error_code=ERR_INVALID_FIELD_TYPE,
            message="'code_hash' must be a string when provided",
            details={"action": action, "field": "code_hash"},
        )
    if isinstance(code_hash, str):
        return {"code_hash": code_hash.strip()}
    return {}


def _validate_self_test_case(item: Any, idx: int) -> Dict[str, Any]:
    action = ACTION_SELF_TEST
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
    return normalized_case


def _validate_self_test(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = ACTION_SELF_TEST
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
    return {"cases": [_validate_self_test_case(item, idx) for idx, item in enumerate(cases)]}


# 动作 -> schema 校验函数。新增动作必须在此登记，否则 validate_action_request 直接 raise。
_VALIDATORS: Final[Dict[str, _ActionValidator]] = {
    ACTION_READ: _validate_read,
    ACTION_MODIFY: _validate_modify,
    ACTION_RUN: _validate_run,
    ACTION_FINALIZE: _validate_finalize,
    ACTION_SELF_TEST: _validate_self_test,
}


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

    handler = _VALIDATORS.get(action)
    if handler is None or not callable(handler):
        # 已进词表但没 schema：这是协议漂移，必须显式失败（旧实现会 fall-through 静默通过）。
        raise ProtocolViolation(
            error_code=ERR_VALIDATOR_MISSING,
            message="action has no registered validator schema",
            details={"action": action, "registered": sorted(_VALIDATORS)},
        )

    normalized: Dict[str, Any] = {"action": action}
    normalized.update(handler(payload) or {})
    return normalized
