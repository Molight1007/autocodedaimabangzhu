"""`spec.input_grammar` 的可执行契约：`parse(stdin) -> dict` 与 `validate(value) -> [violations]`。

这是本设计最贵的单点（设计 §3）：校验门、收缩器、预检三个阶段共用同一份 parser，
只要三处都用本模块，"生成的数据合法"与"判题时能解析"就是同一个判断。

支持的 kind（P1 范围，够覆盖 CCF 前两题）：
- `scalar`      ：读 1 个 token，转 int/float/str。
- `repeat`      ：一维数组，`count` 为字面量或先前 scalar 的名字；`sep` 支持
                  `space`（空白分隔，可跨行）与 `newline`（每个元素独占一行）。
- `matrix`      ：n×m 二维数组（`rows`/`cols` 同上可引用 scalar）。
- `empty_array` ：零长度表达（决策 7 保留 n=0 验收），读一个必须为 0 的计数。

`allow_empty`：`repeat`/`matrix` 显式允许零长度；不放行时 len==0 记违规。

错误口径：解析失败**抛异常**（`GrammarError`），不做"尽力解析"——
哨兵值返回会污染上游判断（设计 §4"证据必须 fail-closed"）。
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Mapping, Optional, Tuple

KIND_SCALAR = "scalar"
KIND_REPEAT = "repeat"
KIND_MATRIX = "matrix"
KIND_EMPTY_ARRAY = "empty_array"
#: 变长记录：只能作为 repeat 的 elem 出现（如"每行先给 k 再给 k 个数"）
KIND_RECORD = "record"
#: 变体记录：只能作为 repeat 的 elem 出现。每行第一个 token 是类型标记（tag），
#: 标记不同 → 后续字段组合不同（如"操作 1 x a / 操作 3 a / 操作 5 x y"）。
KIND_SWITCH = "switch"
ALLOWED_KINDS: tuple[str, ...] = (KIND_SCALAR, KIND_REPEAT, KIND_MATRIX, KIND_EMPTY_ARRAY)

TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_STR = "str"
ALLOWED_ELEM_TYPES: tuple[str, ...] = (TYPE_INT, TYPE_FLOAT, TYPE_STR)

SEP_SPACE = "space"
SEP_NEWLINE = "newline"
ALLOWED_SEPS: tuple[str, ...] = (SEP_SPACE, SEP_NEWLINE)

MAX_ELEMENTS = 5_000_000  # 防止 grammar 被写成无限展开


class GrammarError(ValueError):
    """grammar 定义非法或 stdin 与 grammar 不匹配。`lineno` 为输入行号（1 基）。"""

    def __init__(self, message: str, *, lineno: int = 0, node_index: int = -1) -> None:
        super().__init__(message)
        self.lineno = int(lineno)
        self.node_index = int(node_index)

    def render(self) -> str:
        where = f"第 {self.lineno} 行" if self.lineno else "输入"
        return f"{self}: ({where})"


# ── token 流 ────────────────────────────────────────────────────────────────


class _TokenStream:
    """带行号的 token 游标；`space` 分隔天然支持一行多个，`newline` 靠行号校验。"""

    def __init__(self, text: str) -> None:
        items: List[Tuple[int, str]] = []
        for lineno, raw in enumerate(str(text or "").replace("\r\n", "\n").split("\n"), start=1):
            for token in raw.split():
                items.append((lineno, token))
        self._items = items
        self._cursor = 0

    @property
    def position(self) -> int:
        return self._cursor

    @property
    def remaining(self) -> int:
        return len(self._items) - self._cursor

    def peek(self) -> Optional[Tuple[int, str]]:
        return self._items[self._cursor] if self._cursor < len(self._items) else None

    def take(self, what: str) -> Tuple[int, str]:
        if self._cursor >= len(self._items):
            raise GrammarError(f"输入意外结束：还缺 {what}")
        item = self._items[self._cursor]
        self._cursor += 1
        return item

    def expect_end(self) -> None:
        if self._cursor < len(self._items):
            lineno, token = self._items[self._cursor]
            raise GrammarError(
                f"输入尾部有多余内容 {token!r}（还剩 {self.remaining} 个 token）", lineno=lineno
            )


def _convert(token: str, type_name: str, *, what: str, lineno: int) -> Any:
    if type_name == TYPE_INT:
        try:
            return int(token)
        except ValueError as exc:
            raise GrammarError(f"{what} 需要整数，实际 {token!r}", lineno=lineno) from exc
    if type_name == TYPE_FLOAT:
        try:
            return float(token)
        except ValueError as exc:
            raise GrammarError(f"{what} 需要浮点数，实际 {token!r}", lineno=lineno) from exc
    return token


def _count_expr_names(text: str) -> Optional[set[str]]:
    """解析 count/rows/cols 的受限算术表达式，返回引用到的变量名；非法返回 None。

    只允许：变量名、整数字面量、`+ - * // %`、一元正负、括号。
    禁止函数调用/属性访问/下标/比较，避免借表达式执行任意代码。
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd,
               ast.Load)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif not isinstance(node, allowed):
            return None
    return names


def _eval_count_expr(
    expr: str, parsed: Mapping[str, Any], *, what: str, index: int
) -> int:
    """对已解析的 scalar 求值（顺序依赖：引用必须出现在前面）。"""
    try:
        tree = ast.parse(expr, mode="eval")
        value = _eval_node(tree.body, parsed)
    except KeyError as exc:
        raise GrammarError(
            f"节点[{index}] {what} 表达式引用了未定义的 scalar：{exc}", node_index=index
        ) from exc
    except ZeroDivisionError as exc:
        raise GrammarError(
            f"节点[{index}] {what} 表达式 {expr!r} 除零", node_index=index
        ) from exc
    except (TypeError, ValueError) as exc:
        raise GrammarError(
            f"节点[{index}] {what} 表达式 {expr!r} 非法：{exc}", node_index=index
        ) from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrammarError(
            f"节点[{index}] {what} 表达式 {expr!r} 必须求值为整数，实际 {value!r}",
            node_index=index,
        )
    if value < 0:
        raise GrammarError(
            f"节点[{index}] {what} 表达式 {expr!r} 结果为负数（{value}）", node_index=index
        )
    return int(value)


def _resolve_count(spec_value: Any, parsed: Mapping[str, Any], *, what: str, index: int) -> int:
    if isinstance(spec_value, bool):
        raise GrammarError(f"节点[{index}] {what} 不能是 bool", node_index=index)
    if isinstance(spec_value, int):
        return int(spec_value)
    if isinstance(spec_value, str):
        name = spec_value.strip()
        if name.isdigit():
            return int(name)
        if name in parsed:
            value = parsed[name]
            if not isinstance(value, int) or isinstance(value, bool):
                raise GrammarError(
                    f"节点[{index}] {what} 引用的 {name!r} 不是整数标量", node_index=index
                )
            return int(value)
        # 受限算术表达式（n-1 / n*m / (n-1)//2）：树/图题的父数组、边表长度都靠它
        if _count_expr_names(name) is not None:
            return _eval_count_expr(name, parsed, what=what, index=index)
        raise GrammarError(f"节点[{index}] {what} 引用了未定义的 scalar {name!r}", node_index=index)
    raise GrammarError(f"节点[{index}] {what} 定义非法：{spec_value!r}", node_index=index)


# ── grammar 定义校验 ────────────────────────────────────────────────────────


def normalize_grammar(grammar: Any) -> List[Dict[str, Any]]:
    """接受 list 或 `{"input_grammar": [...]}`；返回节点列表（浅拷贝，不改入参）。"""
    nodes: Any = grammar
    if isinstance(grammar, Mapping):
        nodes = grammar.get("input_grammar", grammar.get("grammar"))
    if not isinstance(nodes, (list, tuple)) or not nodes:
        raise GrammarError("grammar 必须是非空节点列表，或含 input_grammar 字段的 dict")
    result: List[Dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise GrammarError(f"节点[{index}] 不是对象：{node!r}", node_index=index)
        result.append(dict(node))
    return result


def check_spec(grammar: Any) -> List[str]:
    """静态检查 grammar 定义本身；返回问题列表（空表示通过）。"""
    problems: List[str] = []
    try:
        nodes = normalize_grammar(grammar)
    except GrammarError as exc:
        return [str(exc)]
    declared: set[str] = set()
    for index, node in enumerate(nodes):
        kind = str(node.get("kind") or "").strip().lower()
        if kind not in ALLOWED_KINDS:
            problems.append(f"节点[{index}] kind={kind!r} 不在 {ALLOWED_KINDS}")
            continue
        if kind == KIND_SCALAR:
            name = str(node.get("name") or "").strip()
            if not name:
                problems.append(f"节点[{index}] scalar 缺 name")
            etype = str(node.get("type") or TYPE_INT).strip().lower()
            if etype not in ALLOWED_ELEM_TYPES:
                problems.append(f"节点[{index}] scalar type={etype!r} 非法")
            if name:
                declared.add(name)
        elif kind in (KIND_REPEAT, KIND_MATRIX):
            count_key = "count" if kind == KIND_REPEAT else "rows"
            if count_key not in node:
                problems.append(f"节点[{index}] {kind} 缺 {count_key}")
            else:
                ref = node[count_key]
                if isinstance(ref, str):
                    text = ref.strip()
                    if not text.isdigit() and text not in declared:
                        names = _count_expr_names(text)
                        if names is None:
                            problems.append(
                                f"节点[{index}] {count_key} 表达式非法（只允许变量/整数/+-*//%）"
                            )
                        else:
                            unknown = sorted(name for name in names if name not in declared)
                            if unknown:
                                problems.append(
                                    f"节点[{index}] {count_key} 引用未知 scalar {unknown}"
                                )
            if kind == KIND_MATRIX and "cols" not in node:
                problems.append(f"节点[{index}] matrix 缺 cols")
            if kind == KIND_REPEAT:
                sep = str(node.get("sep") or SEP_SPACE).strip().lower()
                if sep not in ALLOWED_SEPS:
                    problems.append(f"节点[{index}] sep={sep!r} 不在 {ALLOWED_SEPS}")
            elem = node.get("elem")
            if not isinstance(elem, Mapping):
                problems.append(f"节点[{index}] {kind} 缺 elem")
            elif str(elem.get("kind") or "").strip().lower() == KIND_SWITCH:
                problems.extend(_check_switch_def(elem, index))
            else:
                etype = str(elem.get("type") or TYPE_INT).strip().lower()
                if etype not in ALLOWED_ELEM_TYPES:
                    problems.append(f"节点[{index}] elem.type={etype!r} 非法")
        elif kind == KIND_EMPTY_ARRAY:
            if not str(node.get("name") or "").strip():
                problems.append(f"节点[{index}] empty_array 缺 name")
    return problems


def _check_switch_def(elem: Mapping[str, Any], index: int) -> List[str]:
    """静态检查 switch 节点定义：tag 名称 + cases 各分支的 fields。"""
    problems: List[str] = []
    if not str(elem.get("tag") or "").strip():
        problems.append(f"节点[{index}] switch 缺 tag（类型标记字段名）")
    cases = elem.get("cases")
    if not isinstance(cases, Mapping) or not cases:
        problems.append(f"节点[{index}] switch 缺 cases（标记值 → 字段组合）")
        return problems
    for case_key, case in cases.items():
        if not str(case_key).strip():
            problems.append(f"节点[{index}] switch 存在空白分支名")
            continue
        fields = (case or {}).get("fields") if isinstance(case, Mapping) else None
        if not isinstance(fields, list):
            problems.append(f"节点[{index}] switch 分支 {case_key!r} 缺 fields")
            continue
        for field_index, field in enumerate(fields):
            if not isinstance(field, Mapping):
                problems.append(f"节点[{index}] switch 分支 {case_key!r} fields[{field_index}] 不是对象")
                continue
            fkind = str(field.get("kind") or KIND_SCALAR).strip().lower()
            if fkind == KIND_SCALAR:
                if not str(field.get("name") or "").strip():
                    problems.append(f"节点[{index}] switch 分支 {case_key!r} fields[{field_index}] 缺 name")
                ftype = str(field.get("type") or TYPE_INT).strip().lower()
                if ftype not in ALLOWED_ELEM_TYPES:
                    problems.append(f"节点[{index}] switch 分支 {case_key!r} 字段 type={ftype!r} 非法")
            elif fkind == KIND_REPEAT:
                etype = str((field.get("elem") or {}).get("type") or TYPE_INT).strip().lower()
                if etype not in ALLOWED_ELEM_TYPES:
                    problems.append(f"节点[{index}] switch 分支 {case_key!r} repeat 字段 elem.type={etype!r} 非法")
            else:
                problems.append(
                    f"节点[{index}] switch 分支 {case_key!r} 字段只支持 scalar/repeat，实际 {fkind!r}"
                )
    return problems


# ── 解析 ────────────────────────────────────────────────────────────────────


def _parse_scalar(node: Mapping[str, Any], stream: _TokenStream, index: int) -> Tuple[str, Any]:
    name = str(node.get("name") or "").strip()
    etype = str(node.get("type") or TYPE_INT).strip().lower()
    lineno, token = stream.take(f"scalar {name or index}")
    return name, _convert(token, etype, what=f"scalar {name}", lineno=lineno)


def _parse_record(
    node: Mapping[str, Any], stream: _TokenStream, outer: Mapping[str, Any],
    *, index: int, label: str,
) -> Dict[str, Any]:
    """解析一条变长记录：字段按顺序读，后面的 count 可引用本条已读的字段。"""
    fields = node.get("fields")
    if not isinstance(fields, list) or not fields:
        raise GrammarError(f"节点[{index}] {label} 的 record 缺 fields", node_index=index)
    local: Dict[str, Any] = dict(outer)
    result: Dict[str, Any] = {}
    for field_index, field in enumerate(fields):
        if not isinstance(field, Mapping):
            raise GrammarError(f"节点[{index}] {label} fields[{field_index}] 不是对象", node_index=index)
        kind = str(field.get("kind") or "").strip().lower()
        if kind == KIND_SCALAR:
            name, value = _parse_scalar(field, stream, field_index)
            local[name] = value
            result[name] = value
        elif kind == KIND_REPEAT:
            values = _parse_repeat(field, stream, local, field_index)
            name = str((field.get("elem") or {}).get("name") or field.get("name") or f"f{field_index}")
            local[name] = values
            result[name] = values
        else:
            raise GrammarError(
                f"节点[{index}] {label} 的 record 字段只支持 scalar/repeat，实际 {kind!r}",
                node_index=index,
            )
    return result


def _parse_switch(
    node: Mapping[str, Any], stream: _TokenStream, outer: Mapping[str, Any],
    *, index: int, label: str,
) -> Dict[str, Any]:
    """解析一条变体记录：先读类型标记，再按标记对应的分支读字段。

    未知标记直接报错（fail-closed）：静默跳过会把后续 token 全部错位。
    """
    tag_name = str(node.get("tag") or "").strip() or "t"
    tag_type = str(node.get("tag_type") or TYPE_INT).strip().lower()
    lineno, token = stream.take(f"{label} 的标记 {tag_name}")
    tag_value = _convert(token, tag_type, what=f"{label}.{tag_name}", lineno=lineno)
    cases = node.get("cases") or {}
    case = cases.get(str(tag_value))
    if case is None:
        case = cases.get(str(token))  # tag_type=int 时 "01" 与 "1" 都算 1 分支
    if case is None:
        case = node.get("default")
    if not isinstance(case, Mapping):
        raise GrammarError(
            f"{label} 的标记 {tag_name}={token!r} 没有对应的字段分支（cases={sorted(cases)}）",
            lineno=lineno, node_index=index,
        )
    local: Dict[str, Any] = dict(outer)
    local[tag_name] = tag_value
    case_fields = case.get("fields") or []
    if not case_fields:  # 分支可以没有后续字段（如"操作 0"单独成行）
        return {tag_name: tag_value}
    fields_result = _parse_record(
        {"fields": case_fields}, stream, local, index=index, label=label
    )
    return {tag_name: tag_value, **fields_result}


def _parse_repeat(
    node: Mapping[str, Any], stream: _TokenStream, parsed: Mapping[str, Any], index: int
) -> List[Any]:
    count = _resolve_count(node.get("count"), parsed, what="count", index=index)
    if count < 0:
        raise GrammarError(f"节点[{index}] count={count} 为负", node_index=index)
    if count > MAX_ELEMENTS:
        raise GrammarError(f"节点[{index}] count={count} 超过上限 {MAX_ELEMENTS}", node_index=index)
    allow_empty = bool(node.get("allow_empty", False))
    if count == 0 and not allow_empty:
        raise GrammarError(
            f"节点[{index}] count=0 但未声明 allow_empty（决策 7：零长度必须显式表达）",
            node_index=index,
        )
    sep = str(node.get("sep") or SEP_SPACE).strip().lower()
    elem = node.get("elem") or {}
    etype = str(elem.get("type") or TYPE_INT).strip().lower()
    elem_name = str(elem.get("name") or "elem")
    elem_kind = str(elem.get("kind") or "").strip().lower()
    if elem_kind == KIND_RECORD:
        return [
            _parse_record(elem, stream, parsed, index=index, label=f"{elem_name}[{offset}]")
            for offset in range(count)
        ]
    if elem_kind == KIND_SWITCH:
        return [
            _parse_switch(elem, stream, parsed, index=index, label=f"{elem_name}[{offset}]")
            for offset in range(count)
        ]
    values: List[Any] = []
    last_line = -1
    for offset in range(count):
        lineno, token = stream.take(f"{elem_name}[{offset}]")
        if sep == SEP_NEWLINE:
            if lineno == last_line:
                raise GrammarError(
                    f"节点[{index}] 要求每个元素独占一行，但 {elem_name}[{offset}] 与上一元素同行",
                    lineno=lineno,
                )
            last_line = lineno
        values.append(_convert(token, etype, what=f"{elem_name}[{offset}]", lineno=lineno))
    return values


def _parse_matrix(
    node: Mapping[str, Any], stream: _TokenStream, parsed: Mapping[str, Any], index: int
) -> List[List[Any]]:
    rows = _resolve_count(node.get("rows"), parsed, what="rows", index=index)
    cols = _resolve_count(node.get("cols"), parsed, what="cols", index=index)
    if rows < 0 or cols < 0:
        raise GrammarError(f"节点[{index}] rows/cols 为负：{rows}×{cols}", node_index=index)
    if rows * cols > MAX_ELEMENTS:
        raise GrammarError(f"节点[{index}] rows×cols={rows * cols} 超过上限", node_index=index)
    allow_empty = bool(node.get("allow_empty", False))
    if (rows == 0 or cols == 0) and not allow_empty:
        raise GrammarError(
            f"节点[{index}] 空矩阵但未声明 allow_empty（决策 7）", node_index=index
        )
    elem = node.get("elem") or {}
    etype = str(elem.get("type") or TYPE_INT).strip().lower()
    grid: List[List[Any]] = []
    for r in range(rows):
        line: List[Any] = []
        for c in range(cols):
            lineno, token = stream.take(f"matrix[{r}][{c}]")
            line.append(_convert(token, etype, what=f"matrix[{r}][{c}]", lineno=lineno))
        grid.append(line)
    return grid


def parse(grammar: Any, text: str) -> Dict[str, Any]:
    """把 stdin 文本解析成 `{name: value}`；不匹配即抛 `GrammarError`。"""
    nodes = normalize_grammar(grammar)
    stream = _TokenStream(text)
    parsed: Dict[str, Any] = {}
    for index, node in enumerate(nodes):
        kind = str(node.get("kind") or "").strip().lower()
        if kind == KIND_SCALAR:
            name, value = _parse_scalar(node, stream, index)
            if name:
                parsed[name] = value
        elif kind == KIND_REPEAT:
            values = _parse_repeat(node, stream, parsed, index)
            name = str((node.get("elem") or {}).get("name") or node.get("name") or f"arr{index}")
            parsed[name] = values
        elif kind == KIND_MATRIX:
            grid = _parse_matrix(node, stream, parsed, index)
            name = str(node.get("name") or node.get("elem_name") or f"mat{index}")
            parsed[name] = grid
        elif kind == KIND_EMPTY_ARRAY:
            lineno, token = stream.take("empty_array 的计数 0")
            if token.strip() not in ("0",):
                raise GrammarError(
                    f"节点[{index}] empty_array 要求计数为 0，实际 {token!r}", lineno=lineno
                )
            parsed[str(node.get("name"))] = []
        else:
            raise GrammarError(f"节点[{index}] kind={kind!r} 不支持", node_index=index)
    stream.expect_end()
    return parsed


# ── 校验 ────────────────────────────────────────────────────────────────────


def _check_elem(value: Any, type_name: str, *, what: str) -> List[str]:
    if type_name == TYPE_INT:
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{what} 不是整数：{value!r}"]
        return []
    if type_name == TYPE_FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{what} 不是浮点数：{value!r}"]
        return []
    if not isinstance(value, str):
        return [f"{what} 不是字符串：{value!r}"]
    return []


def _check_record(node: Mapping[str, Any], value: Any, *, what: str) -> List[str]:
    """校验一条变长记录（字段级）：只检查存在性与标量类型，长度由解析器保证。"""
    violations: List[str] = []
    if not isinstance(value, Mapping):
        return [f"{what} 不是 record(dict)：{type(value).__name__}"]
    for field in node.get("fields") or []:
        kind = str(field.get("kind") or "").strip().lower()
        if kind == KIND_SCALAR:
            name = str(field.get("name") or "").strip()
            if name not in value:
                violations.append(f"{what} 缺字段 {name!r}")
                continue
            etype = str(field.get("type") or TYPE_INT).strip().lower()
            violations.extend(_check_elem(value[name], etype, what=f"{what}.{name}"))
        elif kind == KIND_REPEAT:
            name = str((field.get("elem") or {}).get("name") or field.get("name") or "")
            if name not in value:
                violations.append(f"{what} 缺数组 {name!r}")
                continue
            if not isinstance(value[name], list):
                violations.append(f"{what}.{name} 不是 list")
    return violations


def _check_switch(node: Mapping[str, Any], value: Any, *, what: str) -> List[str]:
    """校验一条变体记录：tag 存在 → 按 tag 找到分支 → 校验该分支字段。"""
    if not isinstance(value, Mapping):
        return [f"{what} 不是 record(dict)：{type(value).__name__}"]
    tag_name = str(node.get("tag") or "t").strip()
    if tag_name not in value:
        return [f"{what} 缺标记字段 {tag_name!r}"]
    cases = node.get("cases") or {}
    case = cases.get(str(value[tag_name]))
    if case is None:
        return [f"{what} 的标记 {tag_name}={value[tag_name]!r} 没有对应分支"]
    return _check_record({"fields": (case or {}).get("fields") or []}, value, what=what)


def validate(grammar: Any, value: Any) -> List[str]:
    """校验已解析结果是否符合 grammar 语义；返回违规描述列表（空表示通过）。"""
    violations: List[str] = []
    nodes = normalize_grammar(grammar)
    if not isinstance(value, Mapping):
        return [f"解析结果不是 dict：{type(value).__name__}"]
    for index, node in enumerate(nodes):
        kind = str(node.get("kind") or "").strip().lower()
        if kind == KIND_SCALAR:
            name = str(node.get("name") or "").strip()
            if name not in value:
                violations.append(f"节点[{index}] 缺 scalar {name!r}")
                continue
            etype = str(node.get("type") or TYPE_INT).strip().lower()
            violations.extend(_check_elem(value[name], etype, what=f"scalar {name}"))
        elif kind == KIND_REPEAT:
            name = str((node.get("elem") or {}).get("name") or node.get("name") or f"arr{index}")
            if name not in value:
                violations.append(f"节点[{index}] 缺数组 {name!r}")
                continue
            items = value[name]
            if not isinstance(items, list):
                violations.append(f"{name} 不是 list：{type(items).__name__}")
                continue
            if not items and not bool(node.get("allow_empty", False)):
                violations.append(f"{name} 为空但未声明 allow_empty")
            expected_count = _safe_count(node.get("count"), value)
            if expected_count is not None and expected_count != len(items):
                violations.append(f"{name} 长度 {len(items)} ≠ 声明 count {expected_count}")
            elem_kind = str((node.get("elem") or {}).get("kind") or "").strip().lower()
            if elem_kind == KIND_RECORD:
                for offset, item in enumerate(items[:MAX_ELEMENTS]):
                    violations.extend(
                        _check_record(node.get("elem") or {}, item, what=f"{name}[{offset}]")
                    )
                    if len(violations) > 200:
                        violations.append("...（违规过多，已截断）")
                        return violations
                continue
            if elem_kind == KIND_SWITCH:
                for offset, item in enumerate(items[:MAX_ELEMENTS]):
                    violations.extend(
                        _check_switch(node.get("elem") or {}, item, what=f"{name}[{offset}]")
                    )
                    if len(violations) > 200:
                        violations.append("...（违规过多，已截断）")
                        return violations
                continue
            etype = str((node.get("elem") or {}).get("type") or TYPE_INT).strip().lower()
            for offset, item in enumerate(items[:MAX_ELEMENTS]):
                violations.extend(_check_elem(item, etype, what=f"{name}[{offset}]"))
                if len(violations) > 200:
                    violations.append("...（违规过多，已截断）")
                    return violations
        elif kind == KIND_MATRIX:
            name = str(node.get("name") or f"mat{index}")
            if name not in value:
                violations.append(f"节点[{index}] 缺矩阵 {name!r}")
                continue
            grid = value[name]
            if not isinstance(grid, list) or any(not isinstance(row, list) for row in grid):
                violations.append(f"{name} 不是二维 list")
                continue
            expected_rows = _safe_count(node.get("rows"), value)
            expected_cols = _safe_count(node.get("cols"), value)
            if expected_rows is not None and expected_rows != len(grid):
                violations.append(f"{name} 行数 {len(grid)} ≠ 声明 rows {expected_rows}")
            if expected_cols is not None:
                for r, row in enumerate(grid):
                    if len(row) != expected_cols:
                        violations.append(f"{name}[{r}] 列数 {len(row)} ≠ 声明 cols {expected_cols}")
                        break
            if (not grid or not grid[0]) and not bool(node.get("allow_empty", False)):
                violations.append(f"{name} 为空矩阵但未声明 allow_empty")
            etype = str((node.get("elem") or {}).get("type") or TYPE_INT).strip().lower()
            for r, row in enumerate(grid[:1000]):
                for c, item in enumerate(row[:1000]):
                    violations.extend(_check_elem(item, etype, what=f"{name}[{r}][{c}]"))
                    if len(violations) > 200:
                        violations.append("...（违规过多，已截断）")
                        return violations
        elif kind == KIND_EMPTY_ARRAY:
            name = str(node.get("name"))
            if name not in value:
                violations.append(f"节点[{index}] 缺 empty_array {name!r}")
            elif value[name] not in ([], None):
                violations.append(f"{name} 应为空数组，实际 {value[name]!r}")
    return violations


def _safe_count(spec_value: Any, parsed: Mapping[str, Any]) -> Optional[int]:
    try:
        return _resolve_count(spec_value, parsed, what="count", index=-1)
    except GrammarError:
        return None


def describe(grammar: Any) -> str:
    """一行摘要，便于日志与 UI 展示（不参与判定）。"""
    try:
        nodes = normalize_grammar(grammar)
    except GrammarError as exc:
        return f"<非法 grammar: {exc}>"
    parts: List[str] = []
    for node in nodes:
        kind = str(node.get("kind") or "?")
        if kind == KIND_SCALAR:
            parts.append(f"{node.get('name')}:{node.get('type', TYPE_INT)}")
        elif kind == KIND_REPEAT:
            elem = node.get("elem") or {}
            if str(elem.get("kind") or "").strip().lower() == KIND_SWITCH:
                parts.append(
                    f"{elem.get('name') or 'ops'}[{node.get('count')}]<switch:{elem.get('tag') or 't'}>"
                )
            else:
                parts.append(
                    f"{elem.get('name') or 'arr'}[{node.get('count')}]{'?' if node.get('allow_empty') else ''}"
                )
        elif kind == KIND_MATRIX:
            parts.append(f"{node.get('name') or 'mat'}[{node.get('rows')}×{node.get('cols')}]")
        else:
            parts.append(f"{node.get('name')}=[]")
    return ", ".join(parts)

# 约束求值已拆到 constraints.py；此处再导出，保持 `grammar.evaluate_constraints` 兼容路径
# __all__ 同时让 pyflakes 识别这两个名字是公开 API，而非无用 import
from .constraints import ConstraintCheck, _eval_node, evaluate_constraints  # noqa: E402
__all__ = ["ConstraintCheck", "evaluate_constraints"]
