from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PythonSourceValidationResult:
    ok: bool
    message: str = ""
    error_type: str = ""
    lineno: Optional[int] = None
    offset: Optional[int] = None
    end_lineno: Optional[int] = None
    end_offset: Optional[int] = None
    formatted_error: str = ""


def validate_python_source(
    source: str,
    *,
    filename: str = "<input>",
    mode: str = "exec",
) -> PythonSourceValidationResult:
    """使用 Python 自身编译器校验源码语法。

    说明：
    - 不引入任何硬编码语法规则，直接复用 Python 解释器的真实解析结果。
    - 返回结构化结果，供 UI/执行器/批量运行器等不同入口统一复用。
    """

    try:
        compile(str(source or ""), filename, mode)
    except SyntaxError as exc:
        return PythonSourceValidationResult(
            ok=False,
            message=str(exc.msg or "invalid syntax"),
            error_type=type(exc).__name__,
            lineno=exc.lineno,
            offset=exc.offset,
            end_lineno=getattr(exc, "end_lineno", None),
            end_offset=getattr(exc, "end_offset", None),
            formatted_error="".join(traceback.format_exception_only(type(exc), exc)),
        )
    except Exception as exc:
        return PythonSourceValidationResult(
            ok=False,
            message=str(exc),
            error_type=type(exc).__name__,
            formatted_error="".join(traceback.format_exception_only(type(exc), exc)),
        )
    return PythonSourceValidationResult(ok=True)
