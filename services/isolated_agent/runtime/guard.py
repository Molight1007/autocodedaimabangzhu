"""Isolated Agent 运行前安全守卫。

核心职责：
- 在代码执行前做 AST 静态检查，尽早拦截危险导入与高风险调用。
- 将“策略拒绝”与“运行失败”分层：命中守卫直接返回 system_error。

不负责：
- 不启动子进程，不做 stdout/stderr 采集。
- 不判断算法正确性（wrong/right 由执行器和校验器负责）。

数据流：
- controller.run -> executor -> 本守卫检查 -> 通过后再进入子进程执行。

约束：
- 守卫只做“高确定性拦截”，避免过度激进导致正常算法代码无法运行。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable, Optional, Set


DEFAULT_BLOCKED_MODULES: Set[str] = {
    "os",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "ctypes",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "telnetlib",
    "pickle",
    "marshal",
}

DEFAULT_BLOCKED_CALLS: Set[str] = {
    "open",
    "exec",
    "eval",
    "compile",
    "input",
    "__import__",
    "breakpoint",
}

DEFAULT_BLOCKED_ATTR_ROOTS: Set[str] = {
    "os",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "ctypes",
}


@dataclass(frozen=True)
class GuardViolation:
    rule: str
    detail: str


def _root_name(name: str) -> str:
    return str(name or "").split(".", 1)[0]


def check_code_safety(
    code: str,
    *,
    blocked_modules: Optional[Iterable[str]] = None,
    blocked_calls: Optional[Iterable[str]] = None,
    blocked_attr_roots: Optional[Iterable[str]] = None,
) -> Optional[GuardViolation]:
    try:
        tree = ast.parse(code or "", mode="exec")
    except SyntaxError as e:
        return GuardViolation(rule="syntax_error", detail=str(e))

    modules = set(blocked_modules or DEFAULT_BLOCKED_MODULES)
    calls = set(blocked_calls or DEFAULT_BLOCKED_CALLS)
    attr_roots = set(blocked_attr_roots or DEFAULT_BLOCKED_ATTR_ROOTS)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_name(alias.name)
                if root in modules:
                    return GuardViolation(rule="blocked_import", detail=root)
            continue

        if isinstance(node, ast.ImportFrom):
            root = _root_name(node.module or "")
            if root in modules:
                return GuardViolation(rule="blocked_import_from", detail=root)
            continue

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in calls:
                return GuardViolation(rule="blocked_call", detail=node.func.id)
            continue

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name) and value.id in attr_roots:
                return GuardViolation(rule="blocked_attr_call", detail=f"{value.id}.{node.func.attr}")
            continue

        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return GuardViolation(rule="blocked_dunder_attr", detail=node.attr)

    return None
