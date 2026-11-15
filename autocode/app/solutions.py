"""解法体系：1~5 份，按题目实际算法定（用户 2026-11-14 定案）。

构成：
- brute（1 份）：最直观、最易写对的解，作小数据金标准；
- primary（1 份）：主解，用于计算全部期望输出；
- alternatives（0~2 份）：不同思路的备选解，用于对拍与教学对比；
- cpp 版本（可选）：语言独立性证据，不算独立“思路”。

LLM 只产出代码，是否采信由样例 + 对拍门决定；plan 只是“写哪几种”的清单。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..engine.runner import compiler_report
from ..prompts import loader as prompts
from . import code_agent
from .prep_models import MAX_BRUTE_ATTEMPTS, MAX_REF_ATTEMPTS, StageFailure
from .prep_rules import statement_excerpt, user_blocks
import json

MAX_SOLUTIONS = 5
MAX_ALTERNATIVES = 2


def _hint(approach: Optional[Dict[str, str]]) -> str:
    if not approach:
        return ""
    return (
        f"{approach.get('name', '')}：{approach.get('idea', '')}"
        f"（复杂度 {approach.get('complexity', '未声明')}）"
    )


def plan(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """让 LLM 给出 1~4 条解法清单；失败返回空表（不阻断主流程）。"""
    try:
        result = service._chat(
            prompts.SOLUTION_PLAN,
            user_blocks(statement_excerpt(problem), spec.get("samples") or []),
            tag="plan#1",
            max_tokens=4096,
        )
    except Exception as exc:  # noqa: BLE001 - 规划失败不阻断主流程
        if exc.__class__.__name__ == "BudgetExhausted":
            raise
        service._note(f"解法规划失败，退化为单解 + 暴力：{exc}")
        return []
    items = (result.json_obj or {}).get("approaches") or []
    approaches: List[Dict[str, str]] = []
    for item in items[:4]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        approaches.append(
            {
                "name": name,
                "idea": str(item.get("idea") or "").strip(),
                "complexity": str(item.get("complexity") or "").strip(),
            }
        )
    return approaches


def _tag(artifact: Any, approach: Optional[Dict[str, str]]) -> None:
    if approach:
        artifact.approach = approach.get("name", "")
        artifact.complexity = approach.get("complexity", "")


def build_all(
    service: Any,
    problem: Dict[str, Any],
    spec: Dict[str, Any],
    samples: List[Dict[str, str]],
    sample_cases: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """按 1~5 份的规则生成解法；返回 key=lang/role 的 RefArtifact 字典。"""
    approaches = plan(service, problem, spec) if getattr(service, "enable_llm_extras", True) else []
    refs: Dict[str, Any] = {}

    try:
        primary = service._build_solution(
            problem, spec, samples, lang="python", role="reference",
            template=prompts.REF_SOLUTION, cases=sample_cases, attempts=MAX_REF_ATTEMPTS,
            approach_hint=_hint(approaches[0] if approaches else None),
        )
    except StageFailure:
        if not getattr(service, "enable_llm_extras", True):
            raise
        service._note("盲写失败，切换到可试跑模式（agent loop）")
        primary = code_agent.solve(service, problem, spec, samples, lang="python")
    _tag(primary, approaches[0] if approaches else None)
    refs["python/reference"] = primary

    if compiler_report().get("cpp") is not None:
        try:
            cpp = service._build_solution(
                problem, spec, samples, lang="cpp", role="reference",
                template=prompts.REF_SOLUTION, cases=sample_cases, attempts=MAX_REF_ATTEMPTS,
                approach_hint=_hint(approaches[0] if approaches else None),
            )
            _tag(cpp, approaches[0] if approaches else None)
            refs["cpp/reference"] = cpp
        except StageFailure as exc:
            # 用户口径：1 份为下限。cpp 写错不该拖垮整题，降级为单语言并记录
            service._note(f"cpp 参考解未通过，降级为单语言：{exc.message[:100]}")
    else:
        service._note("环境缺 g++：cpp 参考解跳过")

    try:
        brute = service._build_solution(
            problem, spec, samples, lang="python", role="brute",
            template=prompts.BRUTE, cases=sample_cases, attempts=MAX_BRUTE_ATTEMPTS,
        )
        brute.approach = "暴力枚举"
        refs["python/brute"] = brute
    except StageFailure as exc:
        # 暴力解只用于小数据对拍；写不出来时降级为"无对拍"，不阻断主解
        service._note(f"暴力解未通过，降级为无对拍：{exc.message[:100]}")

    # 备选解：不同思路；受总份数与单题预算约束
    slots = min(MAX_ALTERNATIVES, max(0, MAX_SOLUTIONS - len(refs)))
    for index, approach in enumerate(approaches[1 : 1 + slots], start=1):
        if service._calls_this_run >= max(1, service.call_cap - 2):
            service._note("备选解跳过：单题调用预算不足")
            break
        try:
            artifact = service._build_solution(
                problem, spec, samples, lang="python", role=f"alt{index}",
                template=prompts.REF_SOLUTION, cases=sample_cases, attempts=2,
                approach_hint=_hint(approach),
            )
        except StageFailure as exc:
            service._note(f"备选解 {approach['name']} 未通过：{exc.message[:120]}")
            continue
        _tag(artifact, approach)
        refs[f"python/alt{index}"] = artifact
    return refs


ALLOWED_TAGS = {
    "暴力枚举", "模拟", "贪心", "二分", "前缀和", "差分", "双指针", "动态规划", "背包",
    "区间DP", "树形DP", "状压DP", "BFS", "DFS", "最短路", "最小生成树", "拓扑排序",
    "并查集", "线段树", "树状数组", "堆", "哈希", "字符串", "数学", "KMP", "Trie",
    "单调栈", "单调队列", "分治", "扫描线", "构造",
}


def _analysis_issues(analysis: Dict[str, Any], samples: List[Dict[str, str]]) -> List[str]:
    issues: List[str] = []
    text = json.dumps(analysis, ensure_ascii=False)
    if "```" in text:
        issues.append("题解包含代码块")
    if not str(analysis.get("plain_question") or "").strip():
        issues.append("缺少 plain_question")
    if not analysis.get("thinking_path"):
        issues.append("缺少 thinking_path")
    for sample in samples:
        answer = str(sample.get("output") or "").strip()
        if answer and answer in text:
            issues.append("题解泄露样例答案")
            break
    return issues


def explain(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """生成题解并落库；失败返回 None（不阻断整题）。"""
    samples = spec.get("samples") or []
    result = service._chat(
        prompts.EXPLANATION,
        user_blocks(statement_excerpt(problem), samples),
        tag="explanation#1",
        max_tokens=4096,
    )
    analysis = result.json_obj or {}
    issues = _analysis_issues(analysis, samples)
    if issues:
        service._note("题解质检：" + "；".join(issues))
        analysis["quality_flags"] = issues
    tags = [tag for tag in (analysis.get("tags") or []) if tag in ALLOWED_TAGS][:5]
    analysis["tags"] = tags
    service.store.save_analysis(int(problem["id"]), analysis, tags)
    return analysis


def similar_local(service: Any, problem_id: int, tags: List[str], limit: int = 3) -> int:
    """在学生题库里按标签重叠找相似题（离线、确定性）。"""
    if not tags:
        return 0
    rows = service.store.db.query_all(
        "SELECT id, title, algorithm_tags FROM problems"
        " WHERE status = 'prepared' AND id != ? AND algorithm_tags IS NOT NULL",
        (problem_id,),
    )
    scored = []
    for row in rows:
        try:
            other = set(json.loads(row["algorithm_tags"] or "[]"))
        except json.JSONDecodeError:
            continue
        overlap = len(set(tags) & other)
        if overlap:
            scored.append((overlap, {"id": row["id"], "title": row["title"], "tags": ",".join(sorted(set(tags) & other))}))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return service.store.save_similar_local(problem_id, [item for _, item in scored[:limit]])


def mark_primary(refs: Dict[str, Any]) -> None:
    """选最快且已验证的非暴力解作为主解（期望输出以它为准）。"""
    best = None
    for artifact in refs.values():
        artifact.is_primary = 0
        if artifact.role == "reference" and artifact.verified:
            if best is None:
                best = artifact
                continue
            current = float(artifact.verify_record.get("sample_max_elapsed_ms") or 1e9)
            incumbent = float(best.verify_record.get("sample_max_elapsed_ms") or 1e9)
            if current < incumbent:
                best = artifact
    if best is not None:
        best.is_primary = 1
