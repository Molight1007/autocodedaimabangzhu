"""代码门：把「模型说的话」变成「可以入库的数据」。

三条原则（与 docs/prompt-standards.md 对齐）：
1. 格式、去重、计数、样例一致性一律代码做，不问模型；
2. 单条不合法就丢单条，整批不可用才整批失败——前者能救回一部分数据；
3. 复核结论必须自洽：说 `pass` 却给出不同的 `recomputed_output`，按 `mismatch` 处理。

本模块不调 LLM、不碰数据库，纯函数，便于单测。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..engine.verify.comparator import compare
from . import config
from .layout_blocks import check_blocks

LAYERS: tuple[str, ...] = ("boundary", "typical", "stress", "trap")
SIZE_CLASSES: tuple[str, ...] = ("tiny", "small", "medium", "large")
#: 层下限：**唯一真源**。提示词里的「硬性下限」那段必须与这里一致，
#: 由 tests/test_slim2_layer_contract.py 逐项比对——两处手写副本漂移过一次，
#: 代价是「压力层」在固定元数的题上永远不可满足（2026-11-21 三数之和）。
LAYER_FLOORS: Dict[str, int] = {"boundary": 3, "typical": 3, "stress": 1, "trap": 1}
#: 规模档的序，只用于比较，不判语义。
SIZE_RANK: Dict[str, int] = {"tiny": 0, "small": 1, "medium": 2, "large": 3}
JUDGE_TYPES: tuple[str, ...] = ("token", "line", "exact", "float", "special", "interactive")
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
DATASET_VERDICTS: tuple[str, ...] = ("pass", "mismatch", "invalid", "uncertain")
ADV_VERDICTS: tuple[str, ...] = ("accept", "reject", "uncertain")
ADV_REASONS: tuple[str, ...] = (
    "ok", "expected_wrong", "input_invalid", "nondeterministic", "precision", "trivial_pass", "other",
)
FAILURE_MODES: tuple[str, ...] = (
    "correct", "WA", "TLE", "RE", "MLE", "none_found", "already_fails_sample",
)

MIN_INPUT_CHARS = 1


def check_layout_coverage(blocks: Sequence[Dict[str, Any]], lines: Sequence[str]) -> Tuple[bool, str]:
    """块必须覆盖本题所有非空行（题名那一行除外）。

    漏行意味着"这段文字不属于任何块"，前端就只能靠猜；而猜错位置比不排版更糟。
    """
    covered = set()
    for block in blocks:
        covered.update(range(int(block["from"]), int(block["to"]) + 1))
    missing = [
        number
        for number, line in enumerate(lines, start=1)
        if number > 1 and str(line).strip() and number not in covered
    ]
    if not missing:
        return True, ""
    preview = "、".join(str(item) for item in missing[:6])
    return False, f"有 {len(missing)} 行没被任何分块覆盖（第 {preview} 行）"
MAX_INPUT_CHARS = config.MAX_INPUT_CHARS  # 唯一真源在 config，保留别名给旧调用
MAX_OUTPUT_CHARS = config.MAX_OUTPUT_CHARS
MAX_ADVERSARIAL_OUTPUT_CHARS = 4_000  # D4 输出预算更紧，单独收紧
MAX_DERIVE_CHARS = 240
MAX_REASON_CHARS = 240  # 错误原因：够写清一行代码怎么错，不写成小作文

#: 空话判据：这些短语没有指向具体行/变量，等于没解释。
_BANNED_REASON = (
    "边界处理不当", "边界没考虑", "边界未考虑", "考虑不周", "逻辑有漏洞",
    "逻辑错误", "代码有问题", "写法不规范", "处理不完善",
)

_BANNED_RECOMPUTE = (
    "与期望一致", "与期望相同", "看起来正确", "看起来对", "经验证", "数据合法",
    "应该没问题", "应该正确", "按题意应是", "无法确定但与",
)


# ── 文本归一 ────────────────────────────────────────────────────────────────
def norm_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def normalize_input(value: Any) -> str:
    """输入归一：统一换行、去行尾空白、去尾部空行、补一个换行。"""
    lines = [line.rstrip() for line in norm_text(value).split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def normalize_output(value: Any) -> str:
    text = norm_text(value).rstrip()
    return text + "\n" if text else ""


def input_hash(value: Any) -> str:
    return hashlib.sha256(normalize_input(value).encode("utf-8")).hexdigest()


def code_hash(value: Any) -> str:
    return hashlib.sha256(norm_text(value).strip().encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


# ── 输出比较 ────────────────────────────────────────────────────────────────
def parse_tolerance(raw: Any) -> float:
    text = _text(raw).lower().replace("eps", "").replace("=", "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        return 1e-6
    return value if value > 0 else 1e-6


#: 本版本**能执行**的判定方式（`JUDGE_TYPES` 的子集）。`unordered` 用于答案顺序不限的题。
SUPPORTED_JUDGES = ("token", "line", "exact", "float", "unordered")

#: `float(1e-6)`、`float（1e-6）`、`float(eps=1e-6)`：模型很爱这么写。
_JUDGE_ARG_RE = re.compile(
    r"^([a-z_]+)\s*[\(\（]\s*(?:eps\s*=\s*)?([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s*[\)\）]$"
)


def normalize_judge(raw: Any) -> Tuple[str, str]:
    """把判定方式拆成 `(基础判定, 容差)`。

    模型经常写 `float(1e-6)`，旧版按精确匹配判成「不支持」：轻则整批数据被拒收，
    重则被降级成 token 比对——浮点答案末位差一点就判错，正确数据被当成错误丢掉。
    这里统一收口成 `float` + `1e-6` 两段，调用方各取所需。

    返回的容差为空串表示原文没写；不认识的基础判定也原样返回，由 `judge_supported` 拒绝。
    """
    text = _text(raw).lower()
    if not text:
        return "token", ""
    matched = _JUDGE_ARG_RE.match(text)
    if matched:
        return matched.group(1), matched.group(2)
    # `float 1e-6` 这种空格写法：只有基础判定本身可用时才当成带参形式
    parts = text.split()
    if len(parts) == 2 and parts[0] in SUPPORTED_JUDGES:
        return parts[0], parts[1]
    return text, ""


def judge_supported(judge_type: str) -> bool:
    base, _tolerance = normalize_judge(judge_type)
    return base in SUPPORTED_JUDGES


def loose_tokens(value: Any) -> List[str]:
    """把答案拆成"与顺序、括号、逗号无关"的记号多重集。

    只用于两处：`unordered` 判定，以及核对"题面样例被转成标准输入后，
    答案还是不是同一个"。`[0,1]`、`0 1`、`0, 1` 在这里等价。
    """
    text = norm_text(value).translate(str.maketrans({c: " " for c in "[](){},'\""}))
    return sorted(text.split())


def compare_output(
    judge_type: str, expected: Any, actual: Any, tolerance: Any = ""
) -> Tuple[bool, str]:
    """返回 `(是否一致, 差异说明)`；未知判定方式抛 ValueError（配置错误必须响）。

    `judge_type` 里自带的容差（`float(1e-6)`）在调用方没显式给 `tolerance` 时生效；
    显式给了就按显式值走——调用方比字符串更清楚这道题要什么精度。
    """
    mode, inline_tolerance = normalize_judge(judge_type)
    mode = mode or "token"
    eps = _text(tolerance) or inline_tolerance
    exp = norm_text(expected)
    act = norm_text(actual)
    if mode == "exact":
        ok = exp.rstrip() == act.rstrip()
        return ok, "" if ok else "exact_mismatch"
    if mode == "float":
        result = compare("float_eps", exp, act, float_eps=parse_tolerance(eps))
        return bool(result.ok), "" if result.ok else str(result.reason or "float_mismatch")
    if mode == "line":
        result = compare("line_trim", exp, act)
        return bool(result.ok), "" if result.ok else str(result.reason or "line_mismatch")
    if mode == "token":
        result = compare("exact", exp, act)
        return bool(result.ok), "" if result.ok else str(result.reason or "token_mismatch")
    if mode == "unordered":
        # 顺序、括号、逗号都不参与比较：`[0,1]` = `[1,0]` = `0 1`
        ok = loose_tokens(exp) == loose_tokens(act)
        return ok, "" if ok else "unordered_mismatch"
    raise ValueError(f"不支持的判定方式 {mode!r}")


# ── 通用 ────────────────────────────────────────────────────────────────────
@dataclass
class Draft:
    """一次模型产物的清洗结果：`fatal` 非空则整批不可用。"""

    ok: bool = True
    fatal: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    items: List[Dict[str, Any]] = field(default_factory=list)

    def fail(self, message: str) -> "Draft":
        self.ok = False
        self.fatal.append(message)
        return self


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _derive_bad(text: str) -> bool:
    if len(text) < 4:
        return True
    return any(token in text for token in _BANNED_RECOMPUTE)


def _reason_bad(text: str) -> bool:
    """错误原因的质量门槛：空话、过短，都判低质量。

    只降置信度不丢弃 —— 原因写得烂，用例本身仍可能是对的；但界面会显示"低质量"，
    让人知道这段解释不可信，而不是当成结论。
    """
    if len(text) < 8:
        return True
    return any(token in text for token in _BANNED_REASON)


def _clean_skips(raw: Any, start: int, end: int, title: str, draft: Draft) -> List[int]:
    """`skip_lines`：题面内部不属于题面的行（导航、状态标签、代码区、评论）。

    只校验范围与类型，不猜"该不该跳"；跳掉一半以上记告警，让人看得见。
    """
    if not isinstance(raw, list):
        return []
    kept: List[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if start < number <= end and number not in kept:
            kept.append(number)
    if start in kept:
        kept.remove(start)  # 标题行不能跳：没有标题的题面无法在库里定位
    if len(kept) > (end - start + 1) // 2:
        draft.warnings.append(f"《{title}》标了 {len(kept)} 行不属于题面，超过一半，请核对")
    return sorted(kept)


# ── D1 切题 ─────────────────────────────────────────────────────────────────
def check_segments(obj: Any, total_lines: int, max_problems: int) -> Draft:
    draft = Draft(payload=_as_dict(obj))
    problems = _as_list(draft.payload.get("problems"))
    if not problems:
        return draft.fail("没有切出任何题目")
    if len(problems) > max_problems:
        # 不截断：切出几道是模型的判断，丢掉多出来的就是静默丢数据，
        # 让人在界面上自己取消不想入库的那几道更安全
        draft.warnings.append(
            f"切出 {len(problems)} 道题，超过单批参考上限 {max_problems} 道，已全部保留；请自行勾选"
        )

    parsed: List[Dict[str, Any]] = []
    for idx, raw in enumerate(problems, start=1):
        row = _as_dict(raw)
        title = _text(row.get("title"))[:120]
        if not title:
            draft.warnings.append(f"第 {idx} 道缺标题，已丢弃")
            continue
        try:
            start = int(row.get("start_line"))
            end = int(row.get("end_line"))
            sample_count = int(row.get("sample_count") or 0)
        except (TypeError, ValueError):
            draft.warnings.append(f"《{title}》行号或样例数不是整数，已丢弃")
            continue
        if start < 1 or end < start or end > total_lines:
            draft.warnings.append(f"《{title}》行号越界 {start}-{end}，已丢弃")
            continue
        judge, inline_tolerance = normalize_judge(row.get("judge_type"))
        if judge not in JUDGE_TYPES:
            draft.warnings.append(f"《{title}》判定方式 {judge!r} 非法，按 token 处理")
            judge = "token"
            inline_tolerance = ""
        tolerance = _text(row.get("tolerance")) or inline_tolerance
        if judge == "float" and not tolerance:
            draft.warnings.append(f"《{title}》浮点题未给容差，按 1e-6 处理")
            tolerance = "1e-6"
        # 约束不截断：漏掉一条真实约束，后面生成的数据就可能越界
        constraints = [_text(item)[:200] for item in _as_list(row.get("constraints")) if _text(item)]
        confidence = _text(row.get("confidence"))
        blocks, block_warnings = check_blocks(row.get("blocks"), end - start + 1, offset=start - 1)
        for warning in block_warnings:
            draft.warnings.append(f"《{title}》{warning}")
        skip_lines = _clean_skips(row.get("skip_lines"), start, end, title, draft)
        parsed.append(
            {
                "title": title,
                "start_line": start,
                "end_line": end,
                "judge_type": judge,
                "tolerance": tolerance[:30],
                "multi_testcase": _flag(row.get("multi_testcase")),
                "constraints": constraints,
                "sample_count": max(0, sample_count),
                "confidence": confidence if confidence in CONFIDENCES else "medium",
                "note": _text(row.get("note"))[:120],
                "skip_lines": skip_lines,
                "blocks": blocks,
            }
        )

    parsed.sort(key=lambda item: item["start_line"])
    kept: List[Dict[str, Any]] = []
    last_end = 0
    for item in parsed:
        if item["start_line"] <= last_end:
            draft.warnings.append(f"《{item['title']}》与上一题区间重叠，已丢弃")
            continue
        kept.append(item)
        last_end = item["end_line"]

    if not kept:
        return draft.fail("所有候选题目都被判非法")
    draft.items = kept
    return draft


# ── D2 数据 ─────────────────────────────────────────────────────────────────
def measured_size(data: Any) -> Tuple[str, int]:
    """按输入实测规模档，不信模型自报的 `size_class`。

    口径与提示词表格一致（元素数 ≤10 / ≤100 / ≤1000 / 更大），元素数按空白
    分隔的 token 计：首行的 n 也算一个 token，分档边界会差 1，不影响判定。
    字符串题（一整行长文本）tokens=1，会落到 tiny——这类题没有规模维度，
    本来就不该用规模构成压力。
    """
    tokens = len(str(data or "").split())
    if tokens <= 10:
        return "tiny", tokens
    if tokens <= 100:
        return "small", tokens
    if tokens <= 1000:
        return "medium", tokens
    return "large", tokens


def check_dataset(
    obj: Any,
    *,
    min_cases: int,
    max_cases: int,
    samples: Sequence[Dict[str, Any]] = (),
    judge_type: str = "token",
    tolerance: str = "",
) -> Draft:
    draft = Draft(payload=_as_dict(obj))
    analysis = _as_dict(draft.payload.get("analysis"))
    plan = _as_dict(draft.payload.get("plan"))
    cases_raw = _as_list(draft.payload.get("cases"))
    draft.payload = {"analysis": analysis, "plan": plan}
    unsupported = _text(analysis.get("unsupported_reason"))
    if unsupported:
        return draft.fail(f"模型判定本题暂不支持：{unsupported[:200]}")
    if not cases_raw:
        # 空 cases 且无说明（同一次拒绝的另一种形状）：带上分析摘要，别只留一句"没有产出"
        hint = _text(analysis.get("answer_model")) or _text(analysis.get("input_model"))
        return draft.fail("模型没有产出任何数据" + (f"（它的分析：{hint[:140]}）" if hint else ""))
    try:
        target = int(plan.get("target_count"))
    except (TypeError, ValueError):
        # 没给计划就按实际条数算：配额该落在数据上，不是落在模型的自报数字上
        target = len(cases_raw)
    if target < min_cases:
        # 下限是产品要求（每题至少 10 组），不是对模型的猜测
        return draft.fail(f"计划条数 {target} 少于下限 {min_cases} 条")
    if target > max_cases:
        draft.warnings.append(f"计划 {target} 条，超过单批参考上限 {max_cases} 条，仍按计划处理")
    if len(cases_raw) != target:
        return draft.fail(f"计划 {target} 条，实际给出 {len(cases_raw)} 条")

    layers = _as_dict(plan.get("layers"))
    try:
        layer_counts = {name: int(layers.get(name) or 0) for name in LAYERS}
    except (TypeError, ValueError):
        return draft.fail("plan.layers 不是整数")
    # 没有计划就不核对"自报的分配"：真正要保证的是数据本身达标（下面按实测分层判），
    # 让模型填一张表再抽查它对不对，只会把合法的题判死。
    if layers and sum(layer_counts.values()) != target:
        return draft.fail(f"plan.layers 之和 {sum(layer_counts.values())} ≠ target_count {target}")

    sample_map = {input_hash(item.get("input")): normalize_output(item.get("output")) for item in samples}
    seen: set[str] = set()
    kept: List[Dict[str, Any]] = []
    for index, raw in enumerate(cases_raw, start=1):
        row = _as_dict(raw)
        layer = _text(row.get("layer"))
        claimed_size = _text(row.get("size_class"))
        data = normalize_input(row.get("input"))
        # 期望输出现在由代码答案执行得出，D2 可以不给；旧版提示词给了也照收，
        # 仅用于"与样例输入撞车时"的一致性检查，不再作为必填项。
        expected = normalize_output(row.get("expected_output"))
        derive = _text(row.get("derive")) or _text(row.get("why"))
        confidence = _text(row.get("confidence")) or "medium"

        if layer not in LAYERS:
            draft.warnings.append(f"第 {index} 条层名 {layer!r} 非法，已丢弃")
            continue
        if not data or len(data) > MAX_INPUT_CHARS:
            draft.warnings.append(f"第 {index} 条输入为空或超过 {MAX_INPUT_CHARS} 字符，已丢弃")
            continue
        if data.count("\n") + 1 > config.MAX_INPUT_LINES:
            draft.warnings.append(f"第 {index} 条输入超过 {config.MAX_INPUT_LINES} 行，已丢弃")
            continue
        if len(expected) > MAX_OUTPUT_CHARS:
            draft.warnings.append(f"第 {index} 条期望输出过长，已丢弃")
            continue
        if len(derive) > MAX_DERIVE_CHARS:
            derive = derive[:MAX_DERIVE_CHARS]
        if confidence not in CONFIDENCES:
            confidence = "medium"
        # 规模以实测为准：模型自报的 size_class 只用来提示它标错了
        size, tokens = measured_size(data)
        if claimed_size and claimed_size != size:
            draft.warnings.append(
                f"第 {index} 条自报 {claimed_size}，实测 {tokens} 个元素（{size}），已按实测记"
            )
        if layer == "boundary" and size not in ("tiny", "small"):
            draft.warnings.append(f"第 {index} 条 boundary 实测 {tokens} 个元素（{size}），已丢弃")
            continue

        key = input_hash(data)
        if key in seen:
            draft.warnings.append(f"第 {index} 条与同批数据重复，已丢弃")
            continue
        seen.add(key)

        if expected and key in sample_map:
            ok, _ = compare_output(judge_type, sample_map[key], expected, tolerance)
            if not ok:
                draft.warnings.append(f"第 {index} 条与题面样例输入相同但输出矛盾，已丢弃")
                continue

        kept.append(
            {
                "layer": layer,
                "size_class": size,
                "input": data,
                "input_hash": key,
                "expected_output": expected,
                "derive": derive,
                "confidence": confidence,
            }
        )

    if len(kept) < min_cases:
        return draft.fail(f"清洗后只剩 {len(kept)} 条，少于下限 {min_cases}")

    _coverage_warnings(kept, draft)
    draft.items = kept
    return draft


def _coverage_warnings(kept: List[Dict[str, Any]], draft: Draft) -> None:
    """覆盖度只出提示，不判失败：合法数据先入库，缺的那层下一轮还能补。"""
    actual = {name: sum(1 for item in kept if item["layer"] == name) for name in LAYERS}
    layer_cn = {"boundary": "边界", "typical": "典型", "stress": "压力", "trap": "陷阱"}
    missing = [
        f"{layer_cn[name]}层 {actual[name]}/{floor}"
        for name, floor in LAYER_FLOORS.items()
        if actual[name] < floor
    ]
    if missing:
        draft.warnings.append(
            "分层覆盖不足：" + "、".join(missing)
            + "。压力层要写到本批最大规模，边界层需 tiny/small"
        )
    others = [SIZE_RANK[item["size_class"]] for item in kept if item["layer"] != "stress"]
    stress = [SIZE_RANK[item["size_class"]] for item in kept if item["layer"] == "stress"]
    if others and stress and max(stress) < max(others):
        rank_cn = {0: "tiny", 1: "small", 2: "medium", 3: "large"}
        draft.warnings.append(
            f"压力层规模不够：其它层已到 {rank_cn[max(others)]}，压力层只到 {rank_cn[max(stress)]}"
            "；压力层应当是本批最大规模"
        )
    if max((SIZE_RANK[item["size_class"]] for item in kept), default=0) == 0:
        draft.warnings.append("本批最大规模只有 tiny（≤10 个元素），无法用规模构成压力")
    outputs = [str(item.get("expected_output") or "") for item in kept]  # 执行后才有
    if all(outputs):
        top = Counter(outputs).most_common(1)[0][1]
        if top * 2 > len(kept):
            draft.warnings.append(f"单一输出出现 {top}/{len(kept)} 次（超过一半），区分度偏低")
    tiny = sum(1 for item in kept if item["size_class"] == "tiny")
    if tiny * 10 > len(kept) * 8:
        draft.warnings.append(f"tiny 占 {tiny}/{len(kept)}，规模偏小")
    draft.payload["layers_actual"] = actual


# ── D3 复核 ─────────────────────────────────────────────────────────────────
def check_dataset_review(
    obj: Any, expected: Sequence[Dict[str, Any]], judge_type: str, tolerance: str
) -> Draft:
    """`expected` 里每条含 `index` 与 `expected_output`（按发送顺序）。"""
    draft = Draft(payload=_as_dict(obj))
    reviews = _as_list(draft.payload.get("reviews"))
    if len(reviews) != len(expected):
        return draft.fail(f"复核返回 {len(reviews)} 条，应为 {len(expected)} 条")

    by_index = {item["index"]: item for item in expected}
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []
    for raw in reviews:
        row = _as_dict(raw)
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            return draft.fail("复核结果含非整数 index")
        if index not in by_index or index in seen:
            return draft.fail(f"复核返回了未知或重复的 index={index}")
        seen.add(index)
        source = by_index[index]

        verdict = _text(row.get("verdict"))
        if verdict not in DATASET_VERDICTS:
            return draft.fail(f"index={index} 的 verdict {verdict!r} 非法")
        recompute = _text(row.get("recompute"))
        recomputed = normalize_output(row.get("recomputed_output"))
        reason = _text(row.get("reason"))[:300]
        corrected = normalize_output(row.get("corrected_output"))

        if _derive_bad(recompute):
            verdict = "uncertain"
            reason = reason or "复核未给出计算过程"

        if verdict == "pass":
            ok, detail = compare_output(judge_type, source["expected_output"], recomputed, tolerance)
            if not ok:
                verdict = "mismatch"
                reason = reason or f"复核结果与期望不一致（{detail}）"
                corrected = recomputed
            if not _flag(row.get("input_ok", True)):
                verdict = "invalid"
                reason = reason or "复核判定输入非法"

        if verdict == "mismatch" and not corrected:
            corrected = recomputed
        if verdict == "mismatch" and not corrected:
            verdict = "uncertain"
            reason = reason or "复核称结果不同但未给出正确值"

        out.append(
            {
                "index": index,
                "verdict": verdict,
                "input_ok": _flag(row.get("input_ok", True)),
                "recompute": recompute[:MAX_DERIVE_CHARS],
                "recomputed_output": recomputed,
                "reason": reason,
                "corrected_output": corrected or None,
            }
        )

    if len(seen) != len(expected):
        return draft.fail("复核结果没有覆盖全部 index")
    out.sort(key=lambda item: item["index"])
    draft.items = out
    return draft


# ── D4 对抗 ─────────────────────────────────────────────────────────────────
def check_adversarial(
    obj: Any,
    *,
    limit: int,
    existing_hashes: Iterable[str] = (),
    max_input_chars: int = MAX_INPUT_CHARS,
) -> Draft:
    draft = Draft(payload=_as_dict(obj))
    diagnosis = _as_dict(draft.payload.get("diagnosis"))
    cases = _as_list(draft.payload.get("cases"))
    draft.payload = {"diagnosis": diagnosis}
    mode = _text(diagnosis.get("failure_mode"))
    if mode not in FAILURE_MODES:
        return draft.fail(f"failure_mode {mode!r} 非法")
    if mode in ("correct", "none_found", "already_fails_sample"):
        draft.items = []
        return draft

    if not cases:
        # 说"有错"却给不出用例：按"本次没有可对抗数据"处理，不当作调用失败
        draft.warnings.append(f"failure_mode={mode} 但没有任何用例，按无可对抗数据处理")
        return draft
    if len(cases) > limit:
        # 只提示不截断：造几条由模型判断，后面的真跑验证会自然筛掉没用的
        draft.warnings.append(f"用例 {len(cases)} 条，超过单批参考上限 {limit} 条，已全部保留")

    seen = set(existing_hashes)
    kept: List[Dict[str, Any]] = []
    for index, raw in enumerate(cases, start=1):
        row = _as_dict(raw)
        data = normalize_input(row.get("input"))
        expected = normalize_output(row.get("expected_output"))
        derive = _text(row.get("derive"))
        if not data or len(data) > max_input_chars:
            draft.warnings.append(f"第 {index} 条输入为空或过长，已丢弃")
            continue
        if not expected or len(expected) > MAX_ADVERSARIAL_OUTPUT_CHARS:
            draft.warnings.append(f"第 {index} 条期望输出为空或过长，已丢弃")
            continue
        weak_derive = _derive_bad(derive)
        if weak_derive:
            draft.warnings.append(f"第 {index} 条推导不足，置信度降为 low")
        reason = _text(row.get("failure_reason"))[:MAX_REASON_CHARS]
        if _reason_bad(reason):
            # 不丢用例：真正判它有没有用的是真跑；这里只标注"解释不可信"
            draft.warnings.append(f"第 {index} 条错误原因不够具体，已标为低质量")
            weak_reason = True
        else:
            weak_reason = False
        key = input_hash(data)
        if key in seen:
            draft.warnings.append(f"第 {index} 条与已有数据重复，已丢弃")
            continue
        seen.add(key)
        confidence = _text(row.get("confidence"))
        if confidence not in CONFIDENCES:
            confidence = "medium"
        kept.append(
            {
                "input": data,
                "input_hash": key,
                "expected_output": expected,
                "predicted_output": normalize_output(row.get("predicted_student_output")),
                "targets": _text(row.get("targets"))[:80],
                "derive": derive,
                "failure_reason": reason,
                "confidence": "low" if (weak_derive or weak_reason) else confidence,
            }
        )
    draft.items = kept
    if not kept:
        draft.warnings.append("所有候选用例都不可用")
    return draft


# ── D5 复核对抗 ─────────────────────────────────────────────────────────────
def check_adversarial_review(
    obj: Any, expected: Sequence[Dict[str, Any]], judge_type: str, tolerance: str
) -> Draft:
    draft = Draft(payload=_as_dict(obj))
    reviews = _as_list(draft.payload.get("reviews"))
    if len(reviews) != len(expected):
        return draft.fail(f"复核返回 {len(reviews)} 条，应为 {len(expected)} 条")

    by_index = {item["index"]: item for item in expected}
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []
    for raw in reviews:
        row = _as_dict(raw)
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            return draft.fail("复核结果含非整数 index")
        if index not in by_index or index in seen:
            return draft.fail(f"复核返回了未知或重复的 index={index}")
        seen.add(index)
        source = by_index[index]

        verdict = _text(row.get("verdict"))
        if verdict not in ADV_VERDICTS:
            return draft.fail(f"index={index} 的 verdict {verdict!r} 非法")
        reason_code = _text(row.get("reason_code")) or "other"
        if reason_code not in ADV_REASONS:
            reason_code = "other"
        recompute = _text(row.get("recompute"))
        recomputed = normalize_output(row.get("recomputed_output"))
        corrected = normalize_output(row.get("corrected_output"))
        reason = _text(row.get("reason"))[:300]

        if _derive_bad(recompute):
            verdict = "uncertain"
            reason_code = "other"
            reason = reason or "复核未给出计算过程"

        if verdict == "accept":
            if not _flag(row.get("input_ok", True)):
                verdict, reason_code = "reject", "input_invalid"
            elif source.get("run_verdict") == "AC":
                verdict, reason_code = "reject", "trivial_pass"
                reason = reason or "学生程序通过了这条用例"
            else:
                ok, detail = compare_output(judge_type, source["expected_output"], recomputed, tolerance)
                if not ok:
                    verdict, reason_code = "reject", "expected_wrong"
                    corrected = corrected or recomputed
                    reason = reason or f"复核结果与期望不一致（{detail}）"

        if verdict == "reject" and reason_code == "expected_wrong" and not corrected:
            verdict, reason_code = "uncertain", "other"
            reason = reason or "称期望错误但未给出正确值"

        out.append(
            {
                "index": index,
                "verdict": verdict,
                "reason_code": reason_code,
                "recompute": recompute[:MAX_DERIVE_CHARS],
                "recomputed_output": recomputed,
                "reason": reason,
                "corrected_output": corrected or None,
            }
        )

    if len(seen) != len(expected):
        return draft.fail("复核结果没有覆盖全部 index")
    out.sort(key=lambda item: item["index"])
    draft.items = out
    return draft


# ── 统计 ────────────────────────────────────────────────────────────────────
def layer_stats(cases: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    return {name: sum(1 for item in cases if item.get("layer") == name) for name in LAYERS}


def summarize_reviews(reviews: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for item in reviews:
        key = str(item.get("verdict") or "")
        result[key] = result.get(key, 0) + 1
    return result


def is_constant_output(outputs: Sequence[str]) -> bool:
    return len({normalize_output(item) for item in outputs}) <= max(1, len(outputs) // 2)


def safe_ratio(part: int, total: int) -> float:
    return float(part) / float(total) if total else 0.0


def looks_like_float(text: str) -> bool:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return False
    return not (math.isnan(value) or math.isinf(value))
