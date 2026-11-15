"""预生成阶段的纯规则：spec 归一化、样例回放、多实现投票、提示词 user 块拼装。

全部是纯函数（不碰 DB、不碰进程），因此可以单测到每个判定分支：
- `normalize_spec`：LLM 返回的 JSON → 内部 spec；不合规项逐条列出（不猜、不默认通过）。
- `replay_samples`：用 spec 里的 grammar 去解析题面样例——这是 spec 的**第一道真门**。
- `agree`：多实现投票，只统计产出成功的实现，全部一致才算一致（不做多数决）。
- `peer_consistent`：某产物是否与对拍基准一致。
"""

from __future__ import annotations

import re

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..engine.datagen import grammar as grammar_engine
from ..engine.datagen import input_parser
from ..engine.verify.comparator import ALLOWED_CHECKERS, compare
from .prep_models import ALLOWED_OUTPUT_TYPES, OUTPUT_FLOAT, OUTPUT_SINGLE

DEFAULT_TIME_LIMITS = {"c": 1000, "cpp": 1000, "python": 4000}
DEFAULT_MEMORY_MB = 512
DEFAULT_FLOAT_EPS = 1e-6


def references_ready(refs: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """所有 reference 产物都必须通过样例 + 对拍；任何一份 verified=0 即视为冲突。

    为什么不能"至少一份通过"：参考解是期望输出的唯一来源，py 与 cpp 结论不一致时
    无法判断谁对；此时放进题库只会把错误答案当标准答案。
    """
    ref_list = [
        artifact for artifact in refs.values()
        if getattr(artifact, "role", "") == "reference"
    ]
    if not ref_list:
        return False, ["<无参考解>"]
    missing = [
        f"{artifact.lang}/{artifact.role}"
        for artifact in ref_list
        if not int(getattr(artifact, "verified", 0) or 0)
    ]
    if missing:
        return False, missing
    # 用户口径（2026-11-14）：1 份为下限、5 份为上限，不强制多语言
    return True, []


def statement_excerpt(problem: Dict[str, Any], limit: int = 12_000) -> str:
    """题面截断（只截长度，不改内容；注入防护靠提示词边界，不靠裁剪）。"""
    return str(problem.get("statement_md") or "")[:limit]


def available_langs() -> List[str]:
    """当前机器可用的判题语言（缺编译器的语言从流水线里降级掉）。"""
    from ..engine.runner import compiler_report

    report = compiler_report()
    return [name for name in ("python", "cpp", "c") if report.get(name)]


def normalize_spec(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """把 LLM 的 spec JSON 归一成内部结构；返回 (spec, 问题列表)。问题非空即视为不合格。"""
    problems: List[str] = []
    if not isinstance(raw, dict) or not raw:
        return {}, ["LLM 未返回 JSON 对象"]

    grammar_node = raw.get("input_grammar")
    problems.extend(f"input_grammar: {item}" for item in grammar_engine.check_spec(grammar_node))
    # count 是 n-1 这类表达式时，n 取最小值就会得到 0；空数组是题面允许的合法形态，
    # LLM 经常忘记声明 allow_empty，这里自动补上（只对含减号的表达式）。
    try:
        nodes = grammar_engine.normalize_grammar(grammar_node)
    except grammar_engine.GrammarError:
        nodes = []
    auto_empty: List[str] = []
    for node in nodes:
        if str(node.get("kind") or "").lower() != "repeat":
            continue
        expr = str(node.get("count") or "")
        if "-" in expr and not node.get("allow_empty"):
            node["allow_empty"] = True
            auto_empty.append(expr)
    # 约束下界为 0 的变量（如 0 <= q <= 20）对应的数组可以是空，自动允许
    zero_vars = set()
    for expr in raw.get("constraints_exec") or []:
        match = re.match(r"\s*0\s*<=\s*([A-Za-z_]\w*)", str(expr)) or re.match(
            r"\s*([A-Za-z_]\w*)\s*>=\s*0", str(expr)
        )
        if match:
            zero_vars.add(match.group(1))
    for node in nodes:
        if str(node.get("kind") or "").lower() != "repeat":
            continue
        if str(node.get("count") or "").strip() in zero_vars and not node.get("allow_empty"):
            node["allow_empty"] = True
            auto_empty.append(f"{node.get('count')}>=0")
    if auto_empty:
        grammar_node = nodes
        raw = dict(raw)
        raw["input_grammar"] = nodes
        raw["auto_allow_empty"] = auto_empty

    output_type = str(raw.get("output_type") or OUTPUT_SINGLE).strip().lower()
    if output_type not in ALLOWED_OUTPUT_TYPES:
        problems.append(f"output_type={output_type!r} 非法（可选 {ALLOWED_OUTPUT_TYPES}）")
        output_type = OUTPUT_SINGLE

    checker = str(raw.get("checker_type") or "exact").strip().lower()
    if checker not in ALLOWED_CHECKERS:
        problems.append(f"checker_type={checker!r} 非法（可选 {ALLOWED_CHECKERS}）")
        checker = "exact"
    if output_type == OUTPUT_FLOAT and checker != "float_eps":
        problems.append("output_type=float_eps 时 checker_type 必须是 float_eps")

    limits: Dict[str, int] = {}
    raw_limits = raw.get("time_limit_ms") or {}
    if isinstance(raw_limits, dict):
        for key in ("c", "cpp", "python"):
            value = raw_limits.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                limits[key] = int(value)
    if not limits:
        limits = dict(DEFAULT_TIME_LIMITS)

    spec: Dict[str, Any] = {
        "input_grammar": grammar_node,
        "constraints_exec": [
            str(item) for item in (raw.get("constraints_exec") or []) if str(item).strip()
        ],
        "output_format": str(raw.get("output_format") or "").strip(),
        "output_type": output_type,
        "checker_type": checker,
        "float_eps": float(raw.get("float_eps") or DEFAULT_FLOAT_EPS),
        "time_limit_ms": limits,
        "memory_limit_mb": int(raw.get("memory_limit_mb") or DEFAULT_MEMORY_MB),
        "traps": [str(item) for item in (raw.get("traps") or [])][:10],
        "tier": (str(raw.get("tier") or "A").strip().upper()[:1] or "A"),
    }
    return spec, problems


def replay_samples(spec: Dict[str, Any], samples: Sequence[Dict[str, str]]) -> List[str]:
    """用 spec 的 grammar 解析题面样例；失败项返回可读错误（喂回 LLM 修复）。"""
    errors: List[str] = []
    for index, sample in enumerate(samples, start=1):
        text = str(sample.get("input") or "")
        if not text.strip():
            continue
        try:
            parsed = input_parser.parse_input(spec, text)
        except grammar_engine.GrammarError as exc:
            errors.append(f"样例{index} 解析失败：{exc.render()}")
            continue
        violations = input_parser.validate_input(spec, parsed)
        if violations:
            errors.append(f"样例{index} 校验失败：{violations[0]}")
    return errors


def agree(values: Dict[str, Optional[str]], checker: str, eps: float) -> Dict[str, Any]:
    """多实现投票：只统计产出成功的实现；全部一致才算一致（不搞多数决）。"""
    available = {key: value for key, value in values.items() if value is not None}
    if not available:
        return {"output": None, "agreeing": 0, "consistent": False}
    keys = sorted(available)
    baseline = available[keys[0]]
    agreeing = 1
    consistent = True
    for key in keys[1:]:
        if compare(checker, baseline, available[key], float_eps=eps).ok:
            agreeing += 1
        else:
            consistent = False
    return {"output": baseline, "agreeing": agreeing, "consistent": consistent}


def peer_consistent(key: str, result: Any, outputs: Dict[str, Dict[str, str]]) -> bool:
    """该产物是否与对拍基准一致：自身是基准则看是否所有候选都对齐它。"""
    if result is None or key not in outputs:
        return False
    if key == "python/reference":
        return bool(outputs) and not result.disagreements and result.missing_cases == 0
    return not any(item.get("candidate") == key for item in result.disagreements)


def user_blocks(statement: str, samples: Sequence[Dict[str, str]]) -> str:
    """拼装 user 消息里的"题面 + 样例"部分（样例最多 3 组，避免超长）。"""
    parts = ["## 题面（原始 Markdown）\n", statement, "\n\n## 题面样例"]
    for index, sample in enumerate(list(samples)[:3], start=1):
        parts.append(f"\n### 样例 {index} 输入\n```\n{str(sample.get('input') or '').strip()}\n```")
        parts.append(f"### 样例 {index} 输出\n```\n{str(sample.get('output') or '').strip()}\n```")
    return "".join(parts)


def spec_block(spec: Dict[str, Any], *, drop_samples: bool = True) -> str:
    """把 spec 渲染进提示词的 JSON 块。"""
    import json

    payload = {key: value for key, value in spec.items() if not (drop_samples and key == "samples")}
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"
