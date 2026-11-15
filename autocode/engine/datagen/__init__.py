"""数据侧纯函数：`grammar`（spec.input_grammar 的 parse/validate）+ 约束求值。

`generator.py` 由 LLM 生成、**必须子进程执行**（`engine.runner.run_case` 传 seed/profile），
本包只提供数据合法性判断，不执行任何 LLM 产物。
"""

from __future__ import annotations

from .grammar import (
    ALLOWED_ELEM_TYPES,
    ALLOWED_KINDS,
    KIND_EMPTY_ARRAY,
    KIND_MATRIX,
    KIND_REPEAT,
    KIND_SCALAR,
    ConstraintCheck,
    GrammarError,
    check_spec,
    describe,
    evaluate_constraints,
    normalize_grammar,
    parse,
    validate,
)

__all__ = [
    "ALLOWED_ELEM_TYPES",
    "ALLOWED_KINDS",
    "ConstraintCheck",
    "GrammarError",
    "KIND_EMPTY_ARRAY",
    "KIND_MATRIX",
    "KIND_REPEAT",
    "KIND_SCALAR",
    "check_spec",
    "describe",
    "evaluate_constraints",
    "normalize_grammar",
    "parse",
    "validate",
]
