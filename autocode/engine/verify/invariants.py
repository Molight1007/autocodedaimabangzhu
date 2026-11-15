"""数据语义不变量：代码可判定的那几类（树 / 排列 / 图 / 有序 / 去重）。

为什么需要：`constraints_exec` 只能表达数值比较，表达不了
"父数组必须构成一棵树""数组必须是 1..n 的排列"这类结构约束。
没有这层，格式合法但语义非法的数据会一路通过。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


def check(invariants: List[str], parsed: Mapping[str, Any]) -> List[str]:
    violations: List[str] = []
    for raw in invariants or []:
        expr = str(raw or "").strip()
        if not expr:
            continue
        kind, _, arg = expr.partition(":")
        kind, arg = kind.strip().lower(), arg.strip()
        checker = _CHECKERS.get(kind)
        if checker is None:
            violations.append(f"未知不变量 {expr!r}（支持 tree/permutation/graph/sorted/unique）")
            continue
        violations.extend(checker(parsed, arg, expr))
    return violations


def _as_list(value: Any) -> bool:
    return isinstance(value, list)


def _check_permutation(parsed: Mapping[str, Any], arg: str, expr: str) -> List[str]:
    arr = parsed.get(arg)
    if not _as_list(arr):
        return [f"{expr}: 字段 {arg!r} 不存在或不是数组"]
    n = parsed.get("n") if isinstance(parsed.get("n"), int) else len(arr)
    if sorted(arr) != list(range(1, n + 1)):
        return [f"{expr}: {arg} 不是 1..{n} 的排列"]
    return []


def _check_tree(parsed: Mapping[str, Any], arg: str, expr: str) -> List[str]:
    parent = parsed.get(arg)
    if not _as_list(parent) or not parent:
        return [f"{expr}: 字段 {arg!r} 不存在或为空"]
    n = parsed.get("n") if isinstance(parsed.get("n"), int) else len(parent) + 1
    if len(parent) not in (n - 1, n):
        return [f"{expr}: {arg} 长度 {len(parent)} 与 n={n} 不匹配（应为 n-1 或 n）"]
    # 归一化：node 1..n → parent[node]
    if len(parent) == n - 1:
        parents = [0] + list(parent)  # parents[1] 对应 node1 的父
    else:
        parents = list(parent)
    for node in range(1, n + 1):
        if node >= len(parents):
            return [f"{expr}: 缺少节点 {node} 的父指针"]
        p = parents[node]
        if p in (0, -1):
            continue
        if not isinstance(p, int) or p < 1 or p > n:
            return [f"{expr}: 节点 {node} 的父 {p!r} 越界"]
    # 每个点沿父链必须能到根，且步数不超过 n（否则有环）
    for node in range(1, n + 1):
        cur, steps = node, 0
        while cur not in (0, -1):
            if not isinstance(cur, int) or cur < 1 or cur > n:
                return [f"{expr}: 节点 {node} 的父链出现非法值 {cur!r}"]
            cur = parents[cur]
            steps += 1
            if steps > n:
                return [f"{expr}: 节点 {node} 的父链成环"]
    return []


def _check_graph(parsed: Mapping[str, Any], arg: str, expr: str) -> List[str]:
    names = [part.strip() for part in arg.split(",") if part.strip()]
    if len(names) != 2:
        return [f"{expr}: graph 需要 'u,v' 两个字段名"]
    us, vs = parsed.get(names[0]), parsed.get(names[1])
    if not _as_list(us) or not _as_list(vs) or len(us) != len(vs):
        return [f"{expr}: {names[0]}/{names[1]} 不存在或长度不一致"]
    n = parsed.get("n") if isinstance(parsed.get("n"), int) else None
    seen = set()
    for u, v in zip(us, vs):
        if not isinstance(u, int) or not isinstance(v, int):
            return [f"{expr}: 边 ({u!r},{v!r}) 端点不是整数"]
        if u == v:
            return [f"{expr}: 出现自环 ({u},{v})"]
        if n is not None and (u < 1 or u > n or v < 1 or v > n):
            return [f"{expr}: 边 ({u},{v}) 端点越界 1..{n}"]
        key = (min(u, v), max(u, v))
        if key in seen:
            return [f"{expr}: 出现重边 {key}"]
        seen.add(key)
    return []


def _check_sorted(parsed: Mapping[str, Any], arg: str, expr: str) -> List[str]:
    arr = parsed.get(arg)
    if not _as_list(arr):
        return [f"{expr}: 字段 {arg!r} 不存在或不是数组"]
    if arr != sorted(arr):
        return [f"{expr}: {arg} 不是非降序"]
    return []


def _check_unique(parsed: Mapping[str, Any], arg: str, expr: str) -> List[str]:
    arr = parsed.get(arg)
    if not _as_list(arr):
        return [f"{expr}: 字段 {arg!r} 不存在或不是数组"]
    if len(set(map(str, arr))) != len(arr):
        return [f"{expr}: {arg} 存在重复元素"]
    return []


_CHECKERS: Dict[str, Any] = {
    "permutation": _check_permutation,
    "tree": _check_tree,
    "graph": _check_graph,
    "sorted": _check_sorted,
    "unique": _check_unique,
}
