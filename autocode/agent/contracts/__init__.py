"""autoCode 协议层导出。

导出目标：
- 常量词表（actions / result types）
- 协议异常与错误码
- 请求校验入口（注册表驱动）
"""

from __future__ import annotations

from .errors import ProtocolViolation
from .protocol import ALLOWED_ACTIONS, RUN_RESULT_TYPES
from .validator import _VALIDATORS, validate_action_request

__all__ = [
    "ALLOWED_ACTIONS",
    "RUN_RESULT_TYPES",
    "ProtocolViolation",
    "_VALIDATORS",
    "validate_action_request",
]
