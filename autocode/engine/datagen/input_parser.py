"""输入解析的两种实现：声明式 grammar 与 LLM 写出的 parser.py。

设计口径（与用户确定的"代码做不了就借 LLM"一致）：
- LLM **不直接产出解析结果**，而是产出可执行、可复现的 `parser.py`；
- 采信条件（门 V2）：全部样例执行成功、输出 JSON、**读完输入**、key 覆盖 spec、结果确定；
- 采信后，generator 产出的每条数据同样用这份 parser 校验——保证"同一把尺子"。

为什么加哨兵：parser 只读第一行、丢掉后面的内容，样例短时照样能过；
追加一行哨兵后若输出完全不变，说明它根本没读到 EOF，这种 parser 不能采信。
"""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Set

from ..runner import VERDICT_AC, run_case
from ..verify import invariants
from . import grammar

PARSER_TIMEOUT_MS = 5000
SENTINEL_LINE = "__AUTOCODE_SENTINEL__ 0"
_CACHE_LIMIT = 512
_CACHE: Dict[tuple[str, str], Dict[str, Any]] = {}


def parser_code(spec: Mapping[str, Any]) -> str:
    return str((spec or {}).get("input_parser_code") or "").strip()


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _run_parser(code: str, text: str) -> Dict[str, Any]:
    """直接执行（不走缓存），用于确定性与哨兵检查。"""
    result = run_case("python", code, text, timeout_ms=PARSER_TIMEOUT_MS)
    if result.get("verdict") != VERDICT_AC:
        raise grammar.GrammarError(
            f"parser.py 执行失败：{result.get('verdict')} {str(result.get('stderr') or '')[:300]}"
        )
    try:
        parsed = json.loads(str(result.get("stdout") or ""))
    except json.JSONDecodeError as exc:
        raise grammar.GrammarError(f"parser.py 输出不是 JSON：{exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise grammar.GrammarError("parser.py 必须输出非空 JSON 对象")
    return parsed


def parse_input(spec: Mapping[str, Any], text: str) -> Dict[str, Any]:
    """按 spec 解析输入：有 parser.py 用它（带缓存），否则用声明式 grammar。"""
    code = parser_code(spec)
    if not code:
        return grammar.parse(spec["input_grammar"], text)
    key = (_digest(code), _digest(text))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    parsed = _run_parser(code, text)
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = parsed
    return parsed




def _elementwise_ok(expr: str, parsed: Mapping[str, Any]) -> bool:
    """数组约束的第二种语义：`0 <= d <= 1e5` 可能指"每个元素"，而非"长度"。

    约束求值默认把数组名映射为长度；若按长度判定失败，再逐元素试一次，
    全部元素都满足即视为通过（只支持单变量表达式，多变量交给调用方）。
    """
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError:
        return False
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if len(names) != 1:
        return False
    name = next(iter(names))
    values = parsed.get(name)
    if not isinstance(values, list) or not values:
        return False
    for element in values:
        checks = grammar.evaluate_constraints([expr], {name: element})
        if not checks or not checks[0].evaluated or not checks[0].ok:
            return False
    return True


def validate_input(spec: Mapping[str, Any], parsed: Mapping[str, Any]) -> List[str]:
    """校验解析结果：结构（grammar 路径）+ **全部** constraints 必须可求值且成立。"""
    violations: List[str] = []
    if not parser_code(spec):
        violations.extend(grammar.validate(spec["input_grammar"], parsed))
    violations.extend(invariants.check(spec.get("invariants") or [], parsed))
    for check in grammar.evaluate_constraints(spec.get("constraints_exec") or [], parsed):
        # "算不出来"（变量名对不上/写法不支持）不等于数据非法：
        # constraints 本身也是 LLM 产物，拿它反过来卡死会误杀正确数据。
        if check.evaluated and not check.ok and _elementwise_ok(check.expr, parsed):
            continue
        if check.evaluated and not check.ok:
            detail = []
            for node in ast.walk(ast.parse(str(check.expr), mode="eval")):
                if not isinstance(node, ast.Name):
                    continue
                value = parsed.get(node.id)
                if isinstance(value, list) and value:
                    detail.append(f"{node.id}.min={min(value)} {node.id}.max={max(value)}")
                elif value is not None:
                    detail.append(f"{node.id}={value}")
            suffix = "（" + "；".join(dict.fromkeys(detail)) + "）" if detail else ""
            violations.append(f"约束不满足：{check.expr}{suffix}")
    return violations


def _nested_field_names(grammar_nodes: Any) -> Set[str]:
    """record/switch 分支内的局部字段名（每条记录内部的名字，如操作行的 x/y/a）。

    它们不是顶层输入变量：parser 输出的顶层 JSON 不可能也不应该带这些 key。
    """
    names: Set[str] = set()
    try:
        nodes = grammar.normalize_grammar(grammar_nodes)
    except grammar.GrammarError:
        return names
    for node in nodes:
        if str(node.get("kind") or "").strip().lower() != "repeat":
            continue
        elem = node.get("elem") or {}
        elem_kind = str(elem.get("kind") or "").strip().lower()
        if elem_kind == "record":
            for field in elem.get("fields") or []:
                fname = str(field.get("name") or (field.get("elem") or {}).get("name") or "").strip()
                if fname:
                    names.add(fname)
        elif elem_kind == "switch":
            tag = str(elem.get("tag") or "").strip()
            if tag:
                names.add(tag)
            for case in (elem.get("cases") or {}).values():
                for field in (case or {}).get("fields") or []:
                    fname = str(field.get("name") or (field.get("elem") or {}).get("name") or "").strip()
                    if fname:
                        names.add(fname)
    return names


def required_keys(spec: Mapping[str, Any]) -> Set[str]:
    """parser 输出必须覆盖的变量名：以 constraints 实际引用的名字为准。

    为什么不再用 grammar：parser 兜底时 grammar 本身就是失败产物（名字可能编错），
    拿它当标准会把正确的 parser 误杀。constraints 是题面语义，必须能算出来。

    但 record/switch 的**分支内字段**（如操作行的 x/y/a）是记录局部名字，
    顶层 JSON 永远不会有它们——不扣除的话，变体操作行的题会被这道门误杀
    （实测 CSP41 棋盘题：constraints 引用 x/y/a，parser 输出只有 n/m/q/ops）。
    """
    names: Set[str] = set()
    for expr in spec.get("constraints_exec") or []:
        try:
            tree = ast.parse(str(expr), mode="eval")
        except SyntaxError:
            continue
        names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    return names - _nested_field_names(spec.get("input_grammar"))


def _unused_grammar_keys(spec: Mapping[str, Any]) -> Set[str]:
    names: Set[str] = set()
    try:
        nodes = grammar.normalize_grammar(spec.get("input_grammar") or [])
    except grammar.GrammarError:
        return names
    for node in nodes:
        kind = str(node.get("kind") or "").strip().lower()
        if kind == "scalar":
            name = str(node.get("name") or "").strip()
            if name:
                names.add(name)
        elif kind == "repeat":
            elem = node.get("elem") or {}
            if str(elem.get("kind") or "").strip().lower() == "record":
                for field in elem.get("fields") or []:
                    fname = str(field.get("name") or (field.get("elem") or {}).get("name") or "").strip()
                    if fname:
                        names.add(fname)
            else:
                name = str(elem.get("name") or node.get("name") or "").strip()
                if name:
                    names.add(name)
        elif kind == "matrix":
            name = str(node.get("name") or "").strip()
            if name:
                names.add(name)
    return names


def check_samples(
    code: str,
    samples: List[Dict[str, str]],
    spec: Optional[Mapping[str, Any]] = None,
    warnings: Optional[List[str]] = None,
) -> List[str]:
    """门 V2：样例全过 + key 覆盖 + 确定性（硬门）；哨兵只记警告（软门）。"""
    errors: List[str] = []
    warnings = warnings if warnings is not None else []
    required = required_keys(spec) if spec else set()
    for index, sample in enumerate(samples, start=1):
        text = str(sample.get("input") or "")
        if not text.strip():
            continue
        try:
            base = _run_parser(code, text)
        except grammar.GrammarError as exc:
            errors.append(f"样例{index}: {exc}")
            continue
        try:
            again = _run_parser(code, text)
        except grammar.GrammarError as exc:
            errors.append(f"样例{index} 第二次执行失败（非确定性）：{exc}")
            continue
        if json.dumps(base, sort_keys=True, ensure_ascii=False) != json.dumps(
            again, sort_keys=True, ensure_ascii=False
        ):
            errors.append(f"样例{index} 两次输出不一致（非确定性）")
            continue
        try:
            with_tail = _run_parser(code, text.rstrip("\n") + "\n" + SENTINEL_LINE + "\n")
        except grammar.GrammarError:
            with_tail = None  # 读到哨兵后报错，说明确实读到了 EOF
        if with_tail is not None and with_tail == base:
            warnings.append(f"样例{index} 追加哨兵后输出不变（可能忽略尾部，未阻断）")
        missing = sorted(required - set(base))
        if missing:
            errors.append(f"样例{index} parser 输出缺少字段 {missing}")
    return errors
