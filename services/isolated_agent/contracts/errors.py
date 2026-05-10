"""Isolated Agent 协议错误定义。

核心职责：
- 定义阶段 1 使用的协议错误码与错误对象，保证调用方可机判处理。
- 将校验失败、状态冲突、动作不合法等问题序列化为统一结构。

不负责：
- 不做动作分发。
- 不做错误恢复策略。

协作关系：
- `contracts.validator` 发现请求问题后抛出 `ProtocolViolation`。
- `controller.dispatcher` 捕获异常并通过 `to_payload()` 回传错误响应。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

ERR_INVALID_ACTION = "invalid_action"
ERR_INVALID_PAYLOAD = "invalid_payload"
ERR_MISSING_FIELD = "missing_field"
ERR_INVALID_FIELD_TYPE = "invalid_field_type"
ERR_FINALIZE_REQUIRES_PASS = "finalize_requires_pass"
ERR_CODE_HASH_MISMATCH = "code_hash_mismatch"
ERR_RUN_EXECUTOR_MISSING = "run_executor_missing"
ERR_RUN_RESULT_INVALID = "run_result_invalid"
ERR_FINALIZE_REQUIRES_SELF_TEST = "finalize_requires_self_test"
ERR_DATA_SAMPLE_TOO_COMPLEX = "data_sample_too_complex"
ERR_DATA_SAMPLE_MISSING = "data_sample_missing"
ERR_VERSION_NOT_FOUND = "version_not_found"


@dataclass(frozen=True)
class ProtocolViolation(Exception):
    """可序列化的协议异常。

    该对象面向跨层传递：validator/controller 抛出后由入口统一转换为 JSON。
    `details` 仅承载可公开诊断信息，禁止写入隐藏测试数据。
    """

    error_code: str
    message: str
    details: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": "protocol_error",
            "error_code": str(self.error_code or ERR_INVALID_PAYLOAD),
            "message": str(self.message or "protocol_violation"),
        }
        if isinstance(self.details, dict) and self.details:
            payload["details"] = dict(self.details)
        return payload
