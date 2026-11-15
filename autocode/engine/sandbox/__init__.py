"""引擎沙箱层。

- `agent_guard.py`：LLM 生成物专用严格守卫（禁 import/input 等）。
- `py_validator.py`：Python 源码语法预检（CE 前置）。
- `student_guard.py`：学生代码专用守卫，P1 新写（必须放行 input/sys.stdin）。
"""

from __future__ import annotations

from .agent_guard import GuardViolation, check_code_safety
from .py_validator import PythonSourceValidationResult, validate_python_source

__all__ = [
    "GuardViolation",
    "PythonSourceValidationResult",
    "check_code_safety",
    "validate_python_source",
]
