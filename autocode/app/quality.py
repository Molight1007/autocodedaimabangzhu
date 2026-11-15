"""题目质量评分（纯函数，契约 §4.1）。

为什么是纯函数：评分必须是"确定性、可复现、可单测"的事（契约 §2 最后一行），
LLM 不参与任何计数；输入只有用例列表与解列表，输出只有 score/issues/统计。

评分规则（写死，改规则必须同步改测试）：
- 基础 100；
- 重复输入占比 > 0        → `-min(20, duplicate_inputs*2)`
- 常量输出占比 > 50%      → `-20`
- 缺 boundary 层          → `-10`
- 缺 stress 层            → `-10`
- 用例总数 < 20           → `-15`
- 已验证解 < 解总数       → `-10`
- **用例 `trust_level='conflict'` > 0 → `-15`（level=error）**：参考解互相矛盾的题不该当可用题
- **用例 `trust_level='reference_only'` > 0 → `-5`（level=warn）**：未独立验证的数据
- 下限 0。

数据可信度两条是 v2 追加规则（契约 §4.1）：只扣"重复/规模"会漏掉更危险的情况——
一道题可能有 5 条参考解互相矛盾的用例，却因为层数和条数达标而拿满分。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Sequence

BASE_SCORE = 100
MAX_DUPLICATE_PENALTY = 20
DUPLICATE_PENALTY_PER_CASE = 2
CONSTANT_OUTPUT_PENALTY = 20
NO_BOUNDARY_PENALTY = 10
NO_STRESS_PENALTY = 10
LOW_TOTAL_PENALTY = 15
UNVERIFIED_PENALTY = 10
# v2 追加：数据可信度（不看这个会把"参考解互相矛盾"的题评成满分）
CONFLICT_CASES_PENALTY = 15
REFERENCE_ONLY_PENALTY = 5
TRUST_CONFLICT = "conflict"
TRUST_REFERENCE_ONLY = "reference_only"
MIN_TOTAL_CASES = 20
CONSTANT_OUTPUT_RATIO = 0.5

LAYER_BOUNDARY = "boundary"
LAYER_STRESS = "stress"

ISSUE_NO_BOUNDARY = "NO_BOUNDARY"
ISSUE_NO_STRESS = "NO_STRESS"
ISSUE_DUPLICATE = "DUPLICATE_INPUTS"
ISSUE_CONSTANT = "CONSTANT_OUTPUTS"
ISSUE_LOW_TOTAL = "LOW_TOTAL_CASES"
ISSUE_UNVERIFIED = "UNVERIFIED_SOLUTIONS"
ISSUE_CONFLICT_CASES = "CONFLICT_CASES"
ISSUE_UNVERIFIED_CASES = "UNVERIFIED_CASES"

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"


def _get(row: Any, key: str, default: Any = None) -> Any:
    """同时支持 sqlite3.Row 与 dict。"""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def score_problem(
    cases: Sequence[Mapping[str, Any] | Any],
    solutions: Sequence[Mapping[str, Any] | Any],
) -> Dict[str, Any]:
    """对一道题的用例与解打分。空用例直接 0 分（并给出 LOW_TOTAL 提示）。"""
    total = len(cases)
    layer_counter: Counter[str] = Counter()
    seen: set[str] = set()
    duplicates = 0
    outputs: List[str] = []
    for case in cases:
        layer = str(_get(case, "layer") or "")
        layer_counter[layer] += 1
        raw_input = _get(case, "input_data")
        if raw_input is None:
            raw_input = _get(case, "input") or ""
        text = str(raw_input)
        if text in seen:
            duplicates += 1
        else:
            seen.add(text)
        expected = _get(case, "expected_output")
        if expected is None:
            expected = _get(case, "output")
        if expected is not None and str(expected).strip():
            outputs.append(str(expected))

    constant_outputs = 0
    if outputs:
        most_common_count = Counter(outputs).most_common(1)[0][1]
        if most_common_count / len(outputs) > CONSTANT_OUTPUT_RATIO:
            constant_outputs = most_common_count

    conflict_cases = 0
    reference_only_cases = 0
    for case in cases:
        trust = str(_get(case, "trust_level") or "").strip().lower()
        if trust == TRUST_CONFLICT:
            conflict_cases += 1
        elif trust == TRUST_REFERENCE_ONLY:
            reference_only_cases += 1

    verified = sum(1 for sol in solutions if int(_get(sol, "verified") or 0))
    total_solutions = len(solutions)
    langs = sorted({str(_get(sol, "lang") or "") for sol in solutions if _get(sol, "lang")})

    score = BASE_SCORE
    issues: List[Dict[str, str]] = []

    if duplicates > 0:
        penalty = min(MAX_DUPLICATE_PENALTY, duplicates * DUPLICATE_PENALTY_PER_CASE)
        score -= penalty
        issues.append(
            {
                "code": ISSUE_DUPLICATE,
                "level": LEVEL_WARN,
                "message": f"存在 {duplicates} 条重复输入（扣 {penalty} 分）",
            }
        )
    if constant_outputs > 0:
        score -= CONSTANT_OUTPUT_PENALTY
        issues.append(
            {
                "code": ISSUE_CONSTANT,
                "level": LEVEL_WARN,
                "message": f"{constant_outputs}/{len(outputs)} 条输出相同，疑似常量输出（扣 {CONSTANT_OUTPUT_PENALTY} 分）",
            }
        )
    if layer_counter.get(LAYER_BOUNDARY, 0) == 0:
        score -= NO_BOUNDARY_PENALTY
        issues.append(
            {
                "code": ISSUE_NO_BOUNDARY,
                "level": LEVEL_WARN,
                "message": f"没有 {LAYER_BOUNDARY} 层（扣 {NO_BOUNDARY_PENALTY} 分）",
            }
        )
    if layer_counter.get(LAYER_STRESS, 0) == 0:
        score -= NO_STRESS_PENALTY
        issues.append(
            {
                "code": ISSUE_NO_STRESS,
                "level": LEVEL_WARN,
                "message": f"没有 {LAYER_STRESS} 层（扣 {NO_STRESS_PENALTY} 分）",
            }
        )
    if total < MIN_TOTAL_CASES:
        score -= LOW_TOTAL_PENALTY
        issues.append(
            {
                "code": ISSUE_LOW_TOTAL,
                "level": LEVEL_WARN,
                "message": f"用例总数 {total} < {MIN_TOTAL_CASES}（扣 {LOW_TOTAL_PENALTY} 分）",
            }
        )
    if verified < total_solutions:
        score -= UNVERIFIED_PENALTY
        issues.append(
            {
                "code": ISSUE_UNVERIFIED,
                "level": LEVEL_WARN,
                "message": f"已验证解 {verified} < 解总数 {total_solutions}（扣 {UNVERIFIED_PENALTY} 分）",
            }
        )

    if conflict_cases > 0:
        score -= CONFLICT_CASES_PENALTY
        issues.append(
            {
                "code": ISSUE_CONFLICT_CASES,
                "level": LEVEL_ERROR,
                "message": f"{conflict_cases} 条用例的参考解互相矛盾",
            }
        )
    if reference_only_cases > 0:
        score -= REFERENCE_ONLY_PENALTY
        issues.append(
            {
                "code": ISSUE_UNVERIFIED_CASES,
                "level": LEVEL_WARN,
                "message": f"{reference_only_cases} 条用例未通过参考解校验",
            }
        )

    return {
        "score": max(0, score),
        "total": total,
        "distinct_inputs": len(seen),
        "duplicate_inputs": duplicates,
        "constant_outputs": constant_outputs,
        "layer_counts": _layer_counts(layer_counter),
        "verified_solutions": verified,
        "solutions": total_solutions,
        "conflict_cases": conflict_cases,
        "reference_only_cases": reference_only_cases,
        "langs": langs,
        "issues": issues,
    }


def _layer_counts(counter: Counter[str]) -> List[Dict[str, Any]]:
    """层计数按固定顺序输出，避免前端每次刷新顺序漂移。"""
    order = {name: index for index, name in enumerate(
        ("sample", "boundary", "exhaustive", "tiny", "small", "mid", "stress", "adversarial", "user")
    )}
    return [
        {"layer": layer, "count": count}
        for layer, count in sorted(counter.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
    ]


def summarize_issues(issues: Iterable[Mapping[str, str]]) -> str:
    """一行摘要（日志/CLI 用）。"""
    return "；".join(str(item.get("message") or "") for item in issues) or "无问题"
