"""`constraints_exec` 的受限求值器（best-effort 数值约束）。

从 grammar.py 拆出（R4 行数门禁：grammar 主攻解析/校验，本模块只管表达式求值）。
只支持数值比较与四则运算（AST 白名单），足以覆盖 "1 <= n <= 1e5" 这类声明；
写不出来/变量未知的约束记 `evaluated=False`，调用方不得据此判数据非法。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Tuple



@dataclass(frozen=True)
class ConstraintCheck:
    """单条 constraints_exec 的求值结果。

    `evaluated=False` 表示"这条写法本模块不支持/变量未知"，属于**未判定**，
    调用方不应据此判定数据非法（best-effort 约束，不能反过来卡死生成）。
    """

    expr: str
    ok: bool
    detail: str = ""
    evaluated: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expr": self.expr,
            "ok": bool(self.ok),
            "detail": self.detail,
            "evaluated": bool(self.evaluated),
        }


_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.Compare,
    ast.Name,
    ast.Constant,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.And,
    ast.Or,
    ast.Load,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def _node_values(values: Mapping[str, Any]) -> Dict[str, Any]:
    """把 scalar 值与数组长度映射成变量：`n`、`len(a)` 之类用裸名代替。"""
    scope: Dict[str, Any] = {}
    for key, item in values.items():
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            scope[key] = item
        elif isinstance(item, list):
            scope[key] = len(item)
            scope[f"len_{key}"] = len(item)
    return scope


def evaluate_constraints(
    exprs: Iterable[str], values: Mapping[str, Any]
) -> List[ConstraintCheck]:
    """逐条求值 `constraints_exec`；不支持的写法记 detail 并视为"未判定通过"。

    只支持数值比较与四则运算（AST 白名单），足以覆盖 "1 <= n <= 1e5" 这类声明。
    """
    scope = _node_values(values)
    results: List[ConstraintCheck] = []
    for raw in exprs or []:
        expr = str(raw or "").strip()
        if not expr:
            continue
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            results.append(ConstraintCheck(expr, False, f"无法解析：{exc.msg}", evaluated=False))
            continue
        bad = [type(node).__name__ for node in ast.walk(tree) if not isinstance(node, _ALLOWED_NODES)]
        if bad:
            results.append(
                ConstraintCheck(expr, False, f"含不支持的语法 {sorted(set(bad))}", evaluated=False)
            )
            continue
        try:
            ok = bool(_eval_node(tree.body, scope))
        except (KeyError, ZeroDivisionError, TypeError, ValueError) as exc:
            results.append(ConstraintCheck(expr, False, f"求值失败：{exc}", evaluated=False))
            continue
        results.append(ConstraintCheck(expr, ok, "" if ok else "约束不满足"))
    return results


def _eval_node(node: ast.AST, scope: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        raise KeyError(f"未知变量 {node.id}")
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, scope)
        if isinstance(node.op, ast.USub):
            return -value
        return +value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, scope)
        right = _eval_node(node.right, scope)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        return left ** right
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(item, scope) for item in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, scope)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, scope)
            if not _apply_cmp(op, left, right):
                return False
            left = right
        return True
    raise ValueError(f"不支持的节点 {type(node).__name__}")


def _apply_cmp(op: ast.AST, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Lt):
        return bool(left < right)
    if isinstance(op, ast.LtE):
        return bool(left <= right)
    if isinstance(op, ast.Gt):
        return bool(left > right)
    return bool(left >= right)

