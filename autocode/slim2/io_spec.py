"""I/O 契约：一道题"程序怎么读、怎么判"的机器可读描述。

## 为什么要有这一层

同一道题，题面写法有无数种（`【样例输入】`、`输入：nums = [...]`、纯表格），
但**评测机只认一种**：一段确定的 stdin 字节流。数据生成的所有环节——造输入、
算期望输出、判定对错——都必须建立在同一个协议上，否则会产出一份"链内自洽、
评测机跑不了"的数据（线上真的发生过：输入 `nums = [1,2], target = 3` 被标成
双模型一致）。

所以协议不能是自由文本，必须是**代码能解释的结构**：模型负责决定协议
（它读得懂题），代码负责按协议校验每一条数据（它不会算错）。

## 结构

```json
{"input": [
   {"name": "n", "type": "int", "min": 1, "max": 100000},
   {"name": "a", "type": "int[]", "len": "n", "min": -1000000000, "max": 1000000000}
 ],
 "output": {"type": "int[]", "order": "any"}}
```

- `type`：`int` / `float` / `str` / `line` / `int[]` / `float[]` / `str[]` / `line[]`
- `len`：数组长度，写整数或前面某个 `int` 字段的名字
- `sep`：数组分隔，`space`（默认，可跨行）或 `line`（每个元素一行）
- `min` / `max`：单个值的取值范围；数组写在元素级
- `output.order = "any"`：答案顺序不限 → 判定方式用 `unordered`

## 一条硬规则

**契约解析不了的数据，一律不入库。** 这不是"多一道检查"，而是唯一能从结构上
排除"伪成功"的地方：只要每条输入都能被同一份契约读出、且没有多余 token，
那这份数据就一定能喂进按同一契约写的程序。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .validators import (
    Draft,
    SUPPORTED_JUDGES,
    _as_dict,
    _as_list,
    _text,
    loose_tokens,
    normalize_input,
    normalize_output,
)

#: 单个值的类型。数组类型由 `<base>[]` 表示，不另开一套。
SCALARS = ("int", "float", "str", "line")
ARRAY_SEPS = ("space", "line")
MAX_FIELDS = 20


def parse(spec: Any) -> Tuple[Dict[str, Any], List[str]]:
    """把模型给的 `io` 规整成可执行结构；返回 `(结构, 问题列表)`。

    问题列表非空就代表"这份协议不能用"：宁可让模型重写一次，
    也不要拿一份含糊的协议去校验数据——含糊的协议等于没有协议。
    """
    problems: List[str] = []
    raw = _as_dict(spec)
    if not raw and isinstance(spec, str) and spec.strip():
        # 有些模型会把 io 再序列化成字符串塞进来；这不是错，解开就行
        try:
            raw = _as_dict(json.loads(spec))
        except (TypeError, ValueError):
            problems.append("io 不是对象，也不是合法的 JSON 字符串")
    rows = _as_list(raw.get("input"))
    if not rows:
        return {}, ["契约没有给出输入字段"]
    if len(rows) > MAX_FIELDS:
        return {}, [f"输入字段有 {len(rows)} 个，超过上限 {MAX_FIELDS}"]

    fields: List[Dict[str, Any]] = []
    seen = set()
    for position, item in enumerate(rows, start=1):
        row = _as_dict(item)
        name = _text(row.get("name")) or f"f{position}"
        kind = _text(row.get("type")) or "int"
        base, is_array = _split_type(kind)
        if base not in SCALARS:
            problems.append(f"第 {position} 个字段 {name} 的类型 {kind!r} 不支持")
            continue
        if name in seen:
            problems.append(f"字段名 {name} 重复")
            continue
        seen.add(name)
        field: Dict[str, Any] = {"name": name, "type": kind, "array": is_array}
        if is_array:
            sep = _text(row.get("sep")) or "space"
            if sep not in ARRAY_SEPS:
                problems.append(f"字段 {name} 的分隔方式 {sep!r} 不支持")
                sep = "space"
            field["sep"] = sep
            # `len_ref` 是规范化之后的键名：parse 必须能吃自己的输出，
            # 否则契约存进库里再读出来就会被判成"没有 len"（不能靠调用方记住口径）。
            length = row.get("len", row.get("len_ref"))
            if isinstance(length, str) and length in seen and length != name:
                field["len_ref"] = length
            elif isinstance(length, int) and length > 0:
                field["len"] = length
            else:
                problems.append(f"数组字段 {name} 没有可用的 len（写整数或前面某个字段名）")
                field["len"] = 1
        low, high = _bounds(row)
        field["min"] = low
        field["max"] = high
        fields.append(field)

    out = _as_dict(raw.get("output"))
    out_type = _text(out.get("type")) or "str"
    if _split_type(out_type)[0] not in SCALARS:
        problems.append(f"输出类型 {out_type!r} 不支持")
    order = _text(out.get("order")) or "fixed"
    return {
        "input": fields,
        "output": {"type": out_type, "order": "any" if order == "any" else "fixed"},
    }, problems


def _split_type(kind: str) -> Tuple[str, bool]:
    text = _text(kind).replace(" ", "")
    if text.endswith("[]"):
        return text[:-2], True
    return text, False


def _bounds(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    def number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    low, high = number(row.get("min")), number(row.get("max"))
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


# ── 校验一份输入 ─────────────────────────────────────────────────────────────

def validate_input(spec: Dict[str, Any], text: str) -> Tuple[bool, str]:
    """按契约读一遍输入；读不通或有多余 token 都算不合法。"""
    fields = spec.get("input") or []
    if not fields:
        return False, "契约没有输入字段"
    tokens = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cursor = 0  # 行游标
    column = 0  # 当前行内已消费的 token 数
    values: Dict[str, Any] = {}

    for field in fields:
        if field["array"]:
            count = _length_of(field, values)
            if count is None:
                return False, f"数组 {field['name']} 的长度引用了未定义的字段"
            if field["sep"] == "line":
                # 前面的标量可能只消费了当前行的开头（`q` + 随后 q 行是同一份输入里
                # 最常见的写法：`3\n2155895064 3\n...`）。此时游标还停在 `q` 那一行，
                # 直接按行切片会把 `q` 自己算成第一行，并多吃掉最后一行。
                if column:
                    cursor += 1
                    column = 0
                if cursor + count > len(tokens):
                    return False, f"数组 {field['name']} 需要 {count} 行，实际不够"
                body = tokens[cursor:cursor + count]
                cursor += count
                items = [line.strip() for line in body]
            else:
                items, cursor, column = _take_tokens(tokens, cursor, column, count)
                if items is None:
                    return False, f"数组 {field['name']} 需要 {count} 个值，实际不够"
            for index, item in enumerate(items, start=1):
                ok, reason = _check_scalar(item, _split_type(field["type"])[0], field)
                if not ok:
                    return False, f"{field['name']}[{index}] {reason}"
            values[field["name"]] = items
            continue

        ok, cursor, column, value = _take_one(tokens, cursor, column, field["type"])
        if not ok:
            return False, f"字段 {field['name']} 读不出来"
        good, reason = _check_scalar(value, field["type"], field)
        if not good:
            return False, f"字段 {field['name']} {reason}"
        values[field["name"]] = value

    if cursor < len(tokens):
        tail = tokens[cursor].split()[column:] + [line for line in tokens[cursor + 1:] if line.strip()]
    else:
        tail = []
    if tail:
        return False, f"读完契约规定的字段后还有多余内容：{tail[0][:40]!r}"
    return True, ""


def _length_of(field: Dict[str, Any], values: Dict[str, Any]) -> Optional[int]:
    if "len" in field:
        return int(field["len"])
    ref = values.get(str(field.get("len_ref") or ""))
    try:
        return int(ref)
    except (TypeError, ValueError):
        return None


def _take_tokens(
    lines: Sequence[str], cursor: int, column: int, count: int
) -> Tuple[Optional[List[str]], int, int]:
    """跨行取 `count` 个空白分隔的 token；不够返回 None。

    只消费到第 `count` 个为止：多吃的 token 会让"多余内容"检查失效
    （一行写了两组数据时，第一组把第二组悄悄吃掉了）。
    """
    items: List[str] = []
    while len(items) < count and cursor < len(lines):
        parts = lines[cursor].split()
        while column < len(parts) and len(items) < count:
            items.append(parts[column])
            column += 1
        if len(items) < count:
            cursor += 1
            column = 0
    if len(items) < count:
        return None, cursor, column
    return items, cursor, column


def _take_one(
    lines: Sequence[str], cursor: int, column: int, kind: str
) -> Tuple[bool, int, int, str]:
    """取一个标量。`line` 取整行剩余内容，其余取一个空白分隔 token。"""
    if cursor >= len(lines):
        return False, cursor, column, ""
    if kind == "line":
        parts = lines[cursor].split()
        rest = " ".join(parts[column:]) if column else lines[cursor]
        return True, cursor + 1, 0, rest.strip()
    while cursor < len(lines):
        parts = lines[cursor].split()
        if column < len(parts):
            return True, cursor, column + 1, parts[column]
        cursor += 1
        column = 0
    return False, cursor, column, ""


def _check_scalar(token: str, kind: str, field: Dict[str, Any]) -> Tuple[bool, str]:
    text = str(token or "").strip()
    if kind == "str":
        return (True, "") if text else (False, "是空的")
    if kind == "line":
        return (True, "") if text else (False, "是空的")
    try:
        number = int(text) if kind == "int" else float(text)
    except (TypeError, ValueError):
        return False, f"不是 {kind}：{text[:20]!r}"
    low, high = field.get("min"), field.get("max")
    if low is not None and number < low:
        return False, f"小于下限 {low:g}"
    if high is not None and number > high:
        return False, f"大于上限 {high:g}"
    return True, ""


# ── 给模型/给人看 ────────────────────────────────────────────────────────────

def describe(spec: Dict[str, Any]) -> str:
    """把契约写成人话：题面页与提示词都用这一份，避免两处口径不一致。"""
    lines: List[str] = []
    for field in spec.get("input") or []:
        label = str(field["name"])
        base = _split_type(str(field["type"]))[0]
        if field["array"]:
            count = field.get("len") or field.get("len_ref") or "?"
            unit = "行" if field.get("sep") == "line" else "个（空格分隔）"
            lines.append(f"- {label}：{count} {unit}，每个是 {base}{_range_text(field)}")
        else:
            lines.append(f"- {label}：一个 {base}{_range_text(field)}")
    out = _as_dict(spec.get("output"))
    order = "，顺序不限" if str(out.get("order")) == "any" else ""
    lines.append(f"输出：{_split_type(str(out.get('type') or 'str'))[0]}{order}")
    return "\n".join(lines)


def _range_text(field: Dict[str, Any]) -> str:
    low, high = field.get("min"), field.get("max")
    if low is None and high is None:
        return ""
    left = "-inf" if low is None else _fmt(low)
    right = "+inf" if high is None else _fmt(high)
    return f"（{left} ≤ x ≤ {right}）"


def _fmt(value: Any) -> str:
    """整数就按整数显示：`1e+09` 这种写法人看着费劲，也容易误读成范围。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def judge_of(spec: Dict[str, Any]) -> str:
    out = _as_dict(spec.get("output"))
    return "unordered" if str(out.get("order")) == "any" else "token"


# ── 契约整体校验（D2 的产物） ────────────────────────────────────────────────

def check_contract(payload: Any, original_samples: Sequence[Dict[str, Any]]) -> Draft:
    """校验 D2 给出的契约与"按契约重写后的样例"。

    样例必须**逐组**转成契约能读的形态：转不了就是契约不成立，
    这时让模型重写，而不是拿原样样例去锚定（锚定必失败，还会掩盖真问题）。
    """
    draft = Draft(payload=_as_dict(payload))
    spec, problems = parse(draft.payload.get("io"))
    if problems:
        for item in problems[:4]:
            draft.fail(item)
        return draft

    judge = _text(draft.payload.get("judge_type")) or judge_of(spec)
    if judge not in SUPPORTED_JUDGES:
        return draft.fail(f"判定方式 {judge!r} 不在支持列表内")
    if judge_of(spec) == "unordered" and judge != "unordered":
        return draft.fail("输出的 order 是 any，判定方式必须用 unordered")

    rows = _as_list(draft.payload.get("samples"))
    if original_samples and len(rows) != len(original_samples):
        return draft.fail(
            f"样例组数不一致：题面 {len(original_samples)} 组，契约下重写 {len(rows)} 组"
        )
    if not rows and original_samples:
        return draft.fail("契约下没有给出样例，无法证明这份协议可行")
    # 无样例题（自编题、老师没给样例）：契约照样成立，只是没有锚点可校准，
    # 这一点由 `run_verification.anchored` 如实标注，不在入口处拦下。

    items: List[Dict[str, Any]] = []
    for index, item in enumerate(rows, start=1):
        row = _as_dict(item)
        data = _text(row.get("input"))
        answer = _text(row.get("output"))
        if not data or not answer:
            return draft.fail(f"第 {index} 组样例的输入或输出为空")
        ok, reason = validate_input(spec, data)
        if not ok:
            # 重试时模型只看得到这句话：把"多列行"的改法直接写进去，
            # 否则它只会微调 min/max，第二轮照样卡在同一处。
            hint = ""
            if any(word in reason for word in ("多余内容", "读不出来", "实际不够")):
                hint = (
                    "；若输入是「T 行、每行多个值」，该字段应写"
                    ' {"type": "line[]", "len": <行数>, "sep": "line"}，代码按行 split'
                )
            return draft.fail(f"第 {index} 组样例不符合它自己的契约：{reason}{hint}")
        if original_samples and index <= len(original_samples):
            before = _text(_as_dict(original_samples[index - 1]).get("output"))
            if before and loose_tokens(before) != loose_tokens(answer):
                return draft.fail(f"第 {index} 组答案在转换后变了：{before!r} → {answer!r}")
        items.append({
            "index": index,
            "input": normalize_input(data),
            "output": normalize_output(answer),
        })

    draft.items = items
    draft.payload = {
        "io": spec,
        "judge_type": judge,
        "samples": items,
        "notes": _text(draft.payload.get("notes")),
    }
    return draft


def loads(raw: Any) -> Dict[str, Any]:
    """从库里存的 JSON 读契约；坏了当没有（不能因为历史脏数据卡住生成）。"""
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict) or not value.get("io"):
        return {}
    spec, problems = parse(value.get("io"))
    if problems:
        return {}
    return {
        "io": spec,
        "judge_type": _text(value.get("judge_type")) or judge_of(spec),
        "samples": _as_list(value.get("samples")),
        "notes": _text(value.get("notes")),
    }


__all__ = [
    "ARRAY_SEPS",
    "SCALARS",
    "check_contract",
    "describe",
    "judge_of",
    "loads",
    "parse",
    "validate_input",
]
