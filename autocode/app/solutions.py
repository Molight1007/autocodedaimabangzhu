"""解法体系：1~5 份，按题目实际算法定（用户 2026-11-14 定案）。

构成：
- brute（1 份）：最直观、最易写对的解，作小数据金标准；
- primary（1 份）：主解，用于计算全部期望输出；
- alternatives（0~2 份）：不同思路的备选解，用于对拍与教学对比；
- cpp 版本（可选）：语言独立性证据，不算独立“思路”。

LLM 只产出代码，是否采信由样例 + 对拍门决定；plan 只是“写哪几种”的清单。
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..engine.runner import compiler_report
from ..prompts import loader as prompts
from . import code_agent
from .prep_models import MAX_BRUTE_ATTEMPTS, MAX_REF_ATTEMPTS, StageFailure
from .prep_rules import statement_excerpt, user_blocks

MAX_SOLUTIONS = 5
MAX_ALTERNATIVES = 2
#: 复数解法的追问提示：只在前一次少于 2 条时用一次，避免无脑多花调用
SECOND_THOUGHT = (
    "\n\n## 补充要求\n"
    "上一次你只给了不到两种解法。请先问自己：\"如果数据范围放大 100 倍，这种做法会死吗？\""
    "——如果会，那就有第二种。\n"
    "请重新输出完整 JSON（不要只给新增的那条）：approaches 必须 2~4 条，"
    "每条都是真正不同的思路，禁止把同一种算法换个名字。"
    "如果确实只有一种可行解，仍只给 1 条，并在 single_reason 写清为什么。"
)


def _hint(approach: Optional[Dict[str, str]]) -> str:
    if not approach:
        return ""
    return (
        f"{approach.get('name', '')}：{approach.get('idea', '')}"
        f"（复杂度 {approach.get('complexity', '未声明')}）"
    )


def _parse_approaches(result: Any) -> Tuple[List[Dict[str, str]], str]:
    """解析解法清单；`single_reason` 是"为什么只有一种"的如实说明。"""
    obj = result.json_obj or {}
    approaches: List[Dict[str, str]] = []
    for item in (obj.get("approaches") or [])[:4]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or any(a["name"] == name for a in approaches):
            continue
        approaches.append(
            {
                "name": name,
                "idea": str(item.get("idea") or "").strip(),
                "complexity": str(item.get("complexity") or "").strip(),
            }
        )
    return approaches, str(obj.get("single_reason") or "").strip()


def plan(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """让 LLM 给 2~4 条解法清单；不足 2 条时追问一次，仍不足则如实记单解。"""
    base = user_blocks(statement_excerpt(problem), spec.get("samples") or [])

    def ask(tag: str, suffix: str = "") -> Any:
        return service._chat(
            prompts.SOLUTION_PLAN, base + suffix, tag=tag, max_tokens=4096
        )

    try:
        approaches, reason = _parse_approaches(ask("plan#1"))
    except Exception as exc:  # noqa: BLE001 - 规划失败不阻断主流程
        if exc.__class__.__name__ == "BudgetExhausted":
            raise
        service._note(f"解法规划失败，退化为单解 + 暴力：{exc}")
        return []

    extra_enabled = bool(getattr(service, "enable_llm_extras", True))
    budget_left = int(getattr(service, "call_cap", 40)) - int(
        getattr(service, "_calls_this_run", 0)
    )
    if len(approaches) < 2 and extra_enabled and budget_left > 4:
        try:
            extra, extra_reason = _parse_approaches(ask("plan#2", SECOND_THOUGHT))
            known = {item["name"] for item in approaches}
            approaches.extend(item for item in extra if item["name"] not in known)
            reason = reason or extra_reason
        except Exception as exc:  # noqa: BLE001 - 追问失败沿用第一次结果
            if exc.__class__.__name__ == "BudgetExhausted":
                raise
            service._note(f"补充解法清单失败，沿用第一次结果：{str(exc)[:100]}")

    if len(approaches) < 2:
        service._note(f"本题只产出一种解法（{reason or '模型未给出第二解'}）")
    return approaches[:4]


def _tag(artifact: Any, approach: Optional[Dict[str, str]]) -> None:
    if approach:
        artifact.approach = approach.get("name", "")
        artifact.complexity = approach.get("complexity", "")


def _normalized_ast(code: str) -> str:
    """把代码压成"只看控制流"的指纹：标识符、常量、函数名一律归一。

    这样 `ans` 改叫 `res`、`ai` 改叫 `a[i]` 不会被当成新解法。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return re.sub(r"\s+", "", code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            node.id = "_"
        elif isinstance(node, ast.arg):
            node.arg = "_"
        elif isinstance(node, ast.FunctionDef):
            node.name = "_"
        elif isinstance(node, ast.Constant):
            node.value = 0
    return ast.dump(tree)


def same_structure(left: str, right: str) -> bool:
    """两份代码是否只是"换壳"：AST 归一后完全相同，或字符相似度 ≥ 0.95。"""
    a, b = _normalized_ast(left), _normalized_ast(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.95


def is_reskin(code: str, refs: Dict[str, Any], lang: str) -> bool:
    """新解是否与已有同语言解换壳。

    注意：**不能按 verified 过滤**。build_all 跑在 _verify_from_cross 之前，
    那时 refs 里所有产物 verified 都是 0；历史 bug 正是加了这个过滤，
    导致主解的换壳版（#26「动态规划」）一路混进题库。
    """
    return any(
        same_structure(code, other.code)
        for other in refs.values()
        if getattr(other, "lang", "") == lang
    )


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
        approach_hint = _hint(approach)
        artifact = None
        for retry in (1, 2):
            try:
                artifact = service._build_solution(
                    problem, spec, samples, lang="python", role=f"alt{index}",
                    template=prompts.REF_SOLUTION, cases=sample_cases, attempts=2,
                    approach_hint=approach_hint,
                )
            except StageFailure as exc:
                service._note(f"备选解 {approach['name']} 未通过：{exc.message[:120]}")
                artifact = None
                break
            # 结构查重：模型常把主解改个变量名就交上来（实测 #26「贪心」= 逐行暴力），
            # 这种"第二种解法"对学生是误导。先要求换控制流重写一次，仍换壳就丢弃。
            reskin = is_reskin(artifact.code, refs, artifact.lang)
            if not reskin:
                break
            service._note(f"备选解「{approach['name']}」与已有解结构相同（第 {retry} 次）")
            if retry == 2:
                artifact = None
                break
            approach_hint += (
                "\n\n## 硬性要求\n上一版与已有解的控制流完全相同，只改了变量名，已作废。"
                "这次必须换一种控制流：例如先排序、先建索引、改变遍历顺序或换数据结构，"
                "不要沿用逐层嵌套扫描。若确实做不到，明确在注释里说明，不要交换壳版本。"
            )
        if artifact is None:
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

#: 题解质检产出的固定问题串。explain 每次重跑都要先清掉上一次的同类标记再合并，
#: 否则干净题解也会永久挂着旧警示（与 enrich 的「方法缺失」同口径）。
ANALYSIS_FLAG_CODES = (
    "题解包含代码块",
    "缺少 plain_question",
    "缺少 thinking_path",
    "题解泄露样例答案",
)


def _norm_ws(text: str) -> str:
    """统一换行与行尾空白。

    历史 bug：样例是 CRLF、题解是 LF，或任一方行尾带空格时，
    `answer in text` 直接不命中 → 真实泄漏被判成干净（probe4 四种变体全漏）。
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in normalized.split("\n")).strip()


def _answer_is_checkable(answer: str) -> bool:
    """短答案（如样例输出只有 `9`）做子串匹配必然误报，必须跳过。

    口径：去空白后 ≥2 行 或 长度 ≥6 才算"可判定的泄漏"。
    """
    text = _norm_ws(answer)
    lines = [line for line in text.split("\n") if line.strip()]
    compact = "".join(text.split())
    if not compact:
        return False
    return len(lines) >= 2 or len(compact) >= 6


def _flatten_text(value: Any) -> str:
    """把分析 JSON 摊平成可子串搜索的纯文本（保留字段内真实换行）。

    注意不能用 `json.dumps` 的结果直接搜索：换行会被转义成 `\\n`，
    多行样例答案（如 `7\\n13`）永远匹配不到。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value)


def _analysis_issues(analysis: Dict[str, Any], samples: List[Dict[str, str]]) -> List[str]:
    issues: List[str] = []
    text = _norm_ws(_flatten_text(analysis))
    if "```" in text:
        issues.append("题解包含代码块")
    if not str(analysis.get("plain_question") or "").strip():
        issues.append("缺少 plain_question")
    if not analysis.get("thinking_path"):
        issues.append("缺少 thinking_path")
    for sample in samples:
        # 两侧都做换行/行尾空白归一化，否则 CRLF 或行尾空格会让真泄漏漏报
        answer = _norm_ws(str(sample.get("output") or ""))
        # 历史 bug：只按 `answer in text` 判断，样例输出是 "9" 时任何含 9 的题解都被判泄漏
        if not _answer_is_checkable(answer):
            continue
        if answer in text:
            issues.append("题解泄露样例答案")
            break
    return issues


def _methods_from_refs(refs: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """解法清单：按"主解 → 备选 → 暴力"排序，同名只留一条。"""
    if not refs:
        return []
    rank = {"reference": 0, "alt1": 1, "alt2": 2, "alt3": 3, "brute": 9}
    ordered = sorted(
        refs.values(),
        key=lambda a: (rank.get(str(getattr(a, "role", "")), 5), str(getattr(a, "approach", ""))),
    )
    methods: List[Dict[str, str]] = []
    for artifact in ordered:
        name = str(getattr(artifact, "approach", "") or "").strip()
        if not name or any(item["name"] == name for item in methods):
            continue
        methods.append(
            {"name": name, "complexity": str(getattr(artifact, "complexity", "") or "未声明")}
        )
    return methods


def _clean_approaches(items: Any, methods: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """按方法清单清洗模型输出：名单外的丢弃、空内容的丢弃、顺序以清单为准。"""
    names = [item["name"] for item in methods]
    parsed: Dict[str, Dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or (names and name not in names):
            continue
        idea = str(item.get("idea") or "").strip()
        correctness = str(item.get("correctness") or "").strip()
        if not idea and not correctness:
            continue
        pitfalls = [str(x).strip() for x in (item.get("pitfalls") or []) if str(x).strip()]
        parsed[name] = {
            "name": name,
            "idea": idea,
            "correctness": correctness,
            "complexity": str(item.get("complexity") or "").strip(),
            "pitfalls": pitfalls[:3],
        }
    if names:
        return [parsed[name] for name in names if name in parsed]
    return list(parsed.values())


def explain(
    service: Any,
    problem: Dict[str, Any],
    spec: Dict[str, Any],
    refs: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """生成题解并落库；失败返回 None（不阻断整题）。

    `refs` 提供已认证的解法清单，题解就会**逐种写法各写一段**（UI 的方法切换靠它）；
    不传时退回"只讲公共思路"的旧口径（测试与补解析入口仍可用）。
    """
    samples = spec.get("samples") or []
    methods = _methods_from_refs(refs)
    user = user_blocks(statement_excerpt(problem), samples)
    if methods:
        user += (
            "\n\n## 已验证的解法（每种各写一段，name 必须逐字一致）\n"
            + json.dumps(methods, ensure_ascii=False)
        )
    result = service._chat(
        prompts.EXPLANATION,
        user,
        tag="explanation#1",
        max_tokens=6144,
    )
    analysis = result.json_obj or {}
    approaches = _clean_approaches(analysis.get("approaches"), methods)
    if approaches:
        analysis["approaches"] = approaches
    else:
        analysis.pop("approaches", None)
    missing = [item["name"] for item in methods if item["name"] not in
               {a["name"] for a in approaches}]
    if missing:
        service._note(f"题解缺少这些方法的独立解析：{missing}")
    issues = _analysis_issues(analysis, samples)
    if issues:
        service._note("题解质检：" + "；".join(issues))
    # 与 enrich 同口径：读旧 → 合并去重，不能整体覆盖（否则会抹掉“方法解析缺失”等已有标记）
    row = service.store.db.query_one(
        "SELECT analysis_json FROM problems WHERE id=?", (int(problem["id"]),)
    )
    previous: Dict[str, Any] = {}
    if row is not None and row["analysis_json"]:
        try:
            previous = json.loads(row["analysis_json"])
        except json.JSONDecodeError:
            previous = {}
    # 本次没写出按方法解析时，保留上一次的，别把已有内容清空（只加不减）
    if not analysis.get("approaches") and previous.get("approaches"):
        analysis["approaches"] = previous["approaches"]
    raw_flags = previous.get("quality_flags")
    merged = [str(x) for x in raw_flags] if isinstance(raw_flags, list) else []
    # 先清掉上一次题解质检写的同类标记（本次是干净题解时不能继续挂警示），
    # 其它来源（如 enrich 的「方法缺失」）原样保留。
    merged = [item for item in merged if item not in ANALYSIS_FLAG_CODES]
    for issue in issues:
        if issue not in merged:
            merged.append(issue)
    if merged:
        analysis["quality_flags"] = merged
    else:
        analysis.pop("quality_flags", None)
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
