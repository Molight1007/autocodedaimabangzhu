"""补齐两类内容：按方法解析、逐行代码解析；并可补生成指定语言答案。"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from ..prompts import loader as prompts
from ..store.db import default_db_path
from . import judge, line_notes
from .prep_models import StageFailure
from .prep_rules import spec_block, statement_excerpt, user_blocks


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """连**当前生效**的库（`AUTOCODE_DB` 优先），并在退出时提交/回滚 + 关闭。

    历史问题：这里曾硬编码 `ROOT/"data"/"autocode.db"`，绕过 `AUTOCODE_DB`——
    用副本库跑生成时，补语言/补解析被写进了真实库，还留下孤儿行。
    现在统一走 `store.db.default_db_path()`，与 api/prep 完全同一口径。
    """
    conn = sqlite3.connect(default_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _solutions(pid: int) -> List[Dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT lang, role, approach, complexity, code, line_notes_json FROM reference_solutions"
            " WHERE problem_id=? AND verified=1 ORDER BY is_primary DESC, role, lang", (pid,)
        )]


def _methods(solutions: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    seen, out = set(), []
    for sol in solutions:
        name = sol["approach"] or "未标注方法"
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "complexity": sol["complexity"] or "未声明", "role": sol["role"]})
    return out


#: 按方法解析：1 次初试 + 最多 2 次带反馈重试（缺字段是模型最常犯的错，值得再要一次）。
MAX_METHOD_ATTEMPTS = 3
#: 每个方法必须有实际内容的字段（空串等于"没写"，不能静默入库）。
METHOD_REQUIRED_FIELDS = ("idea", "correctness")
#: 写进 `analysis_json.quality_flags` 的文案模板：必须是自解释的一句话
#:（该数组由前端逐条渲染，也供 CLI/导出直接阅读）。
METHOD_MISSING_FLAG = "方法「{name}」解析缺失"
#: 用于清理上一次 enrich 写下的同类旧标记（本次字段已补齐时不能继续告警）。
_METHOD_MISSING_RE = re.compile(r"^方法「.*」解析缺失$")


def _note(service: Any, message: str) -> None:
    """`service` 可能是测试桩（无 `_note`），有就记、没有就跳过。"""
    note = getattr(service, "_note", None)
    if callable(note):
        note(message)


def _missing_method_fields(
    by_name: Dict[str, Dict[str, Any]], methods: List[Dict[str, str]]
) -> Dict[str, List[str]]:
    """逐条校验清单里的方法是否都给了非空 idea/correctness；返回 {方法名: [缺失字段]}。"""
    missing: Dict[str, List[str]] = {}
    for method in methods:
        item = by_name.get(method["name"]) or {}
        fields = [name for name in METHOD_REQUIRED_FIELDS if not str(item.get(name) or "").strip()]
        if fields:
            missing[method["name"]] = fields
    return missing


def _keep(new: Any, old: Any) -> str:
    """新值非空则用新值，否则保留旧值（**不覆盖库里的非空字段**）。"""
    fresh = str(new or "").strip()
    if fresh:
        return fresh
    return str(old or "").strip()


def enrich_problem(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(problem["id"])
    solutions = _solutions(pid)
    methods = _methods(solutions)
    if not methods:
        return {"ok": False, "reason": "没有已验证的解"}

    base = user_blocks(statement_excerpt(problem), spec.get("samples") or []) + "\n\n## 已有解法\n" + json.dumps(
        methods, ensure_ascii=False
    ) + "\n\n## 约束\n" + spec_block(spec)
    by_name: Dict[str, Dict[str, Any]] = {}
    missing: Dict[str, List[str]] = {}
    feedback = ""
    for attempt in range(1, MAX_METHOD_ATTEMPTS + 1):
        result = service._chat(
            prompts.METHOD_ANALYSIS, base + feedback,
            tag=f"method_analysis#{pid}", max_tokens=4096, json_mode=True,
        )
        payload = result.json_obj or {}
        got = payload.get("methods") or []
        by_name = {
            str(item.get("name") or "").strip(): item
            for item in got
            if isinstance(item, dict)
        }
        missing = _missing_method_fields(by_name, methods)
        if not missing:
            break
        detail = "；".join(f"{name} 缺 {'/'.join(fields)}" for name, fields in missing.items())
        feedback = (
            f"\n\n上一次输出不合格：\n{detail}\n"
            "请按「已有解法」清单逐条重出完整 JSON：methods 的条数与 name 必须与清单完全一致，"
            "idea 与 correctness 都必须非空。"
        )
        _note(service, f"按方法解析第 {attempt}/{MAX_METHOD_ATTEMPTS} 次缺字段：{detail}")

    with _db() as conn:
        row = conn.execute("SELECT analysis_json FROM problems WHERE id=?", (pid,)).fetchone()
        analysis = json.loads(row["analysis_json"]) if row and row["analysis_json"] else {}
    previous = {
        str(item.get("name") or ""): item
        for item in (analysis.get("approaches") or [])
        if isinstance(item, dict)
    }
    approaches = []
    for method in methods:
        item = by_name.get(method["name"]) or {}
        old = previous.get(method["name"]) or {}
        approaches.append({
            "name": method["name"],
            "idea": _keep(item.get("idea"), old.get("idea")),
            "correctness": _keep(item.get("correctness"), old.get("correctness")),
            "complexity": _keep(item.get("complexity"), old.get("complexity")) or method["complexity"],
            "pitfalls": [str(x) for x in (item.get("pitfalls") or old.get("pitfalls") or [])][:3],
        })

    # 重试用尽仍缺字段：不伪造内容，把缺失情况记进 quality_flags（合并去重）并回传
    # 口径（与前端约定）：`quality_flags` 每项都是**自解释的人话**，
    # 前端逐条渲染，所以这里不写裸方法名（裸名用户看不出"它怎么了"）。
    # `flagged` 保留裸方法名，供脚本/报表做结构化消费。
    flagged = sorted(missing) if missing else []
    analysis["approaches"] = approaches
    # 标记有生命周期：先清掉上一次 enrich 写下的「方法缺失」旧标记（本次已补齐的不该继续告警），
    # 再合并本次缺失；其它来源的人话标记（如题解质检）原样保留。
    raw_flags = analysis.get("quality_flags")
    merged = [str(x) for x in raw_flags] if isinstance(raw_flags, list) else []
    merged = [x for x in merged if not _METHOD_MISSING_RE.match(x)]
    for name in flagged:
        message = METHOD_MISSING_FLAG.format(name=name)
        if message not in merged:
            merged.append(message)
    if merged:
        analysis["quality_flags"] = merged
    else:
        analysis.pop("quality_flags", None)
    if flagged:
        _note(service, f"按方法解析仍缺字段，已标记 quality_flags：{flagged}")

    with _db() as conn:
        conn.execute(
            "UPDATE problems SET analysis_json=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), pid),
        )

    notes_done = 0
    for sol in solutions:
        if line_notes.is_current_notes(sol["line_notes_json"]):
            continue
        code = str(sol["code"] or "")[:12000]
        body = (
            user_blocks(statement_excerpt(problem), spec.get("samples") or [])
            + "\n\n## 方法\n"
            + json.dumps(
                {"name": sol["approach"] or "未标注方法",
                 "complexity": sol["complexity"] or "未声明"},
                ensure_ascii=False,
            )
            + f"\n\n## 目标语言\n{sol['lang']}\n\n## 带行号的完整代码（L<n> 为真实行号）\n"
            + line_notes.number_code(code)
        )
        res = service._chat(
            prompts.CODE_NOTES, body,
            tag=f"code_notes#{pid}_{sol['lang']}_{sol['role']}",
            max_tokens=line_notes.NOTES_MAX_TOKENS, json_mode=True,
        )
        payload = line_notes.format_line_notes(res.json_obj or {}, code)
        if not payload:
            continue
        with _db() as conn:
            conn.execute(
                "UPDATE reference_solutions SET line_notes_json=? WHERE problem_id=? AND lang=? AND role=?",
                (json.dumps(payload, ensure_ascii=False), pid, sol["lang"], sol["role"]),
            )
        notes_done += 1
    return {"ok": True, "methods": len(approaches), "notes": notes_done, "flagged": flagged}


def add_language(service: Any, problem: Dict[str, Any], spec: Dict[str, Any], lang: str) -> Dict[str, Any]:
    pid = int(problem["id"])
    samples = spec.get("samples") or []
    cases = [{"id": f"sample{i}", "input": s.get("input", ""), "expected": s.get("output", "")} for i, s in enumerate(samples, 1)]
    try:
        artifact = service._build_solution(
            problem, spec, samples, lang=lang, role="reference",
            template=prompts.REF_SOLUTION, cases=cases, attempts=6,
        )
    except StageFailure:
        from . import code_agent
        artifact = code_agent.solve(service, problem, spec, samples, lang=lang, max_steps=6)
    if not artifact.approach:
        with _db() as conn:
            base = conn.execute(
                "SELECT approach, complexity FROM reference_solutions"
                " WHERE problem_id=? AND role='reference' AND approach!='' LIMIT 1", (pid,)
            ).fetchone()
        if base:
            artifact.approach, artifact.complexity = base["approach"], base["complexity"] or artifact.complexity
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM reference_solutions WHERE problem_id=? AND lang=? AND role='reference'", (pid, lang)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE reference_solutions SET code=?, verified=1, verification_level=?, approach=?,"
                " complexity=?, line_notes_json=NULL WHERE id=?",
                (artifact.code, artifact.verification_level, artifact.approach, artifact.complexity, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO reference_solutions(problem_id, lang, role, code, verified, verification_level, approach, complexity, is_primary)"
                " VALUES(?,?,?,?,1,?,?,?,0)",
                (pid, lang, "reference", artifact.code, artifact.verification_level,
                 artifact.approach, artifact.complexity),
            )
    return {"ok": True, "lang": lang, "chars": len(artifact.code)}


def judge_all(pid: int, spec: Dict[str, Any]) -> bool:
    """用已存解回放样例，确认新生成的语言答案真的能跑。"""
    cases = [{"id": f"s{i}", "input": s.get("input", ""), "expected": s.get("output", "")}
             for i, s in enumerate(spec.get("samples") or [], 1)]
    with _db() as conn:
        solutions = [
            dict(row)
            for row in conn.execute(
                "SELECT lang, code FROM reference_solutions"
                " WHERE problem_id=? AND role='reference' AND verified=1",
                (pid,),
            )
        ]
    for sol in solutions:
        batch = judge.run_solution(
            sol["lang"], sol["code"], cases, checker_type=spec.get("checker_type", "exact")
        )
        if not batch.all_accepted:
            return False
    return True
