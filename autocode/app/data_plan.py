"""数据计划：LLM 决定"造几组、每组多少条、覆盖什么"，本地照单生成。

为什么单独成模块：`prep_service.py` 受 800 行门禁约束，而这份逻辑（提示词调用、
计划校验、特征去重、覆盖核对）独立可测，不依赖流水线其它阶段。

设计口径（与对抗数据一致）：**模型只提计划，不定答案**。
- 模型给 `layer`（boundary/exhaustive/stress）、`profile`（tiny/small/mid/max/edge）、
  `count`、`focus`，以及可选的小规模 `hand_cases`；
- 输入是否合法由 `grammar + constraints` 判，期望输出由已验证参考解算；
- 组内按结构特征去重，避免 10 条几乎一样的小随机数据冒充"多组覆盖"。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..prompts import loader as prompts

#: 生成器支持的档位（见 `generator.md`），越界一律丢弃
PROFILES: Tuple[str, ...] = ("tiny", "small", "mid", "max", "edge")
#: 题库分层：与 `test_cases.layer` 的既有语义保持一致
LAYERS: Tuple[str, ...] = ("boundary", "exhaustive", "stress")
#: 每档允许的分层（防止模型写出"boundary + max"这种自相矛盾的组）
ALLOWED: Dict[str, Tuple[str, ...]] = {
    "boundary": ("edge", "tiny"),
    "exhaustive": ("tiny", "small", "mid"),
    "stress": ("max",),
}

MIN_TOTAL = 10
MAX_TOTAL = 40
MAX_GROUPS = 5
MAX_HAND_CASES = 6
MAX_HAND_LINES = 40
MAX_GROUP_COUNT = 10


def default_plan(boundary: int, exhaustive: int, stress: int) -> Dict[str, Any]:
    """未启用模型（或模型失败）时的确定性计划：与旧行为逐档对齐。"""
    groups = [
        {"name": "边界", "layer": "boundary", "profile": "edge",
         "count": max(1, int(boundary)), "focus": ["最小规模", "极值"]},
        {"name": "小规模", "layer": "exhaustive", "profile": "tiny",
         "count": max(1, int(exhaustive)), "focus": ["常规小数据"]},
        {"name": "大规模", "layer": "stress", "profile": "max",
         "count": max(1, int(stress)), "focus": ["触达上界"]},
    ]
    return {
        "total": sum(g["count"] for g in groups),
        "groups": groups,
        "hand_cases": [],
        "must_cover": [],
        "reason": "未启用模型数据计划，按默认档位生成",
        "source": "default",
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_str(value: Any, limit: int = 40) -> str:
    return str(value or "").strip()[:limit]


def _focus_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value[:3]:
        text = _clean_str(item, 40)
        if text and text not in out:
            out.append(text)
    return out


def normalize_plan(payload: Any, fallback: Dict[str, Any], *, min_total: int = MIN_TOTAL) -> Dict[str, Any]:
    """校验模型计划：非法项丢弃、越界修回、总数不足补一组。

    返回结构恒可用（`groups` 非空），`source` 标明是否真来自模型，便于如实汇报。
    """
    if not isinstance(payload, dict):
        return dict(fallback, source="default")

    groups: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(payload.get("groups") or []):
        if not isinstance(item, dict):
            continue
        layer = _clean_str(item.get("layer"), 16).lower()
        profile = _clean_str(item.get("profile"), 16).lower()
        if layer not in LAYERS or profile not in PROFILES:
            continue
        if profile not in ALLOWED[layer]:
            continue
        count = min(MAX_GROUP_COUNT, max(1, _as_int(item.get("count"), 1)))
        name = _clean_str(item.get("name"), 20) or f"第{index + 1}组"
        if name in seen_names:
            continue
        seen_names.add(name)
        groups.append({
            "name": name,
            "layer": layer,
            "profile": profile,
            "count": count,
            "focus": _focus_list(item.get("focus")),
        })
        if len(groups) >= MAX_GROUPS:
            break

    if not groups:
        return dict(fallback, source="default")

    # 总数封顶：从最大的一组往下削，保证既守上限、又保留小档覆盖
    while sum(g["count"] for g in groups) > MAX_TOTAL:
        biggest = max(groups, key=lambda g: g["count"])
        if biggest["count"] <= 1:
            break
        biggest["count"] -= 1

    total = sum(g["count"] for g in groups)
    if total < min_total:
        groups.append({
            "name": "补充小规模",
            "layer": "exhaustive",
            "profile": "tiny",
            "count": min_total - total,
            "focus": ["补足条数下限"],
        })
        total = min_total

    hand: List[Dict[str, str]] = []
    seen_inputs: set[str] = set()
    for item in payload.get("hand_cases") or []:
        if len(hand) >= MAX_HAND_CASES or not isinstance(item, dict):
            continue
        text = str(item.get("input") or "")
        if not text.strip() or len(text) > 20000:
            continue
        if len([line for line in text.strip().split("\n") if line.strip()]) > MAX_HAND_LINES:
            continue
        if text in seen_inputs:
            continue
        seen_inputs.add(text)
        hand.append({"input": text, "focus": _clean_str(item.get("focus"), 60)})

    cover: List[str] = []
    for item in payload.get("must_cover") or []:
        text = _clean_str(item, 80)
        if text and text not in cover:
            cover.append(text)
        if len(cover) >= 6:
            break

    return {
        "total": total,
        "groups": groups,
        "hand_cases": hand,
        "must_cover": cover,
        "reason": _clean_str(payload.get("reason"), 120),
        "source": "llm",
    }


def request_plan(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """一次 LLM 调用拿数据计划；失败返回 None（调用方退回默认计划，不阻断整题）。"""
    from ..engine.datagen import grammar

    material = (
        "## 题面\n" + str(problem.get("statement_md") or "")[:5000]
        + "\n\n## 输入格式 grammar\n" + grammar.describe(spec.get("input_grammar"))
        + "\n\n## 约束\n" + str(spec.get("constraints_exec") or [])
        + "\n\n## 样例（前 2 组）\n"
        + str((spec.get("samples") or [])[:2])
    )
    try:
        result = service._chat(
            prompts.DATASETS, material, tag="data_plan#1", max_tokens=2048, json_mode=True
        )
        return result.json_obj
    except Exception as exc:  # noqa: BLE001 - 模型不可用时不能卡住数据阶段
        if exc.__class__.__name__ in ("BudgetExhausted", "StageFailure"):
            raise
        service._note(f"数据计划调用失败，退回默认档位：{str(exc)[:120]}")
        return None


def _numbers(text: str) -> List[int]:
    out: List[int] = []
    for raw in re.findall(r"-?\d+", text):
        try:
            out.append(int(raw))
        except ValueError:
            continue
        if len(out) >= 4000:
            break
    return out


def signature(text: str) -> Tuple[int, int, int, int, int, int]:
    """结构指纹：规模档、数值量级、取值密度、负/零、行是否全同。

    只用分桶后的粗粒度，目的是"别让 10 条几乎一样的随机数据占满名额"，
    不做语义判断，也不需要理解 grammar。
    """
    lines = [line for line in text.strip().split("\n") if line.strip()]
    values = _numbers(text)
    line_bucket = min(6, max(0, len(lines).bit_length() - 1)) if lines else 0
    magnitude = 0
    if values:
        biggest = max(abs(v) for v in values)
        while biggest >= 10 and magnitude < 9:
            biggest //= 10
            magnitude += 1
    ratio = len(set(values)) / max(1, len(values))
    density = min(5, int(ratio * 5))
    has_neg = 1 if any(v < 0 for v in values) else 0
    has_zero = 1 if any(v == 0 for v in values) else 0
    same_lines = 1 if len(lines) > 2 and len(set(lines)) == 1 else 0
    return (line_bucket, magnitude, density, has_neg, has_zero, same_lines)


def diverse_select(
    cases: Sequence[Dict[str, Any]],
    limit: int,
    seen_hashes: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """组内挑 `limit` 条：先按结构指纹去重，再按需补齐（不伪造、不重复）。"""
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    picked: List[Dict[str, Any]] = []
    signatures: set[Tuple[int, ...]] = set()
    for case in cases:
        if len(picked) >= limit:
            break
        text = str(case.get("input") or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        if digest in seen_hashes:
            continue
        sig = signature(text)
        if sig in signatures:
            continue
        signatures.add(sig)
        seen_hashes.add(digest)
        picked.append(case)
    if len(picked) < limit:
        for case in cases:
            if len(picked) >= limit:
                break
            text = str(case.get("input") or "")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            picked.append(case)
    return picked


def hand_case_ok(spec: Dict[str, Any], text: str) -> bool:
    """小规模手写用例的格式门：能解析、约束成立才算候选（不看答案）。"""
    from ..engine.datagen import grammar as grammar_engine
    from ..engine.datagen import input_parser

    try:
        parsed = input_parser.parse_input(spec, text)
    except Exception:  # noqa: BLE001 - 任何解析异常都按不合法处理
        return False
    if input_parser.validate_input(spec, parsed):
        return False
    checks = grammar_engine.evaluate_constraints(spec.get("constraints_exec") or [], parsed)
    return not any(check.evaluated and not check.ok for check in checks)


def summary(plan: Dict[str, Any], actual: Dict[str, int]) -> Dict[str, Any]:
    """落库用的精简计划 + 实际产出（供 UI 如实展示"计划 vs 实际"）。"""
    covered = sum(n for layer, n in actual.items() if layer != "sample")
    return {
        "source": plan.get("source") or "default",
        "reason": plan.get("reason") or "",
        "total_planned": plan.get("total") or 0,
        "total_actual": covered,
        "groups": [
            {
                "name": g.get("name") or "",
                "layer": g.get("layer") or "",
                "profile": g.get("profile") or "",
                "count": g.get("count") or 0,
                "focus": list(g.get("focus") or []),
            }
            for g in (plan.get("groups") or [])
        ],
        "hand_cases": len(plan.get("hand_cases") or []),
        "must_cover": list(plan.get("must_cover") or []),
        "actual": dict(actual),
    }


def group_seed(index: int) -> int:
    """每组独立的种子基线，避免不同组撞出同一批数据。"""
    return 500 + max(0, int(index)) * 1000


def attempt_budget(profile: str, count: int) -> int:
    """生成尝试数：小档多试几条做去重，大档按需即可（max 很贵）。"""
    extra = 3 if profile in ("tiny", "small", "edge") else 1
    return min(MAX_GROUP_COUNT * 2, max(count, 1) + extra)
