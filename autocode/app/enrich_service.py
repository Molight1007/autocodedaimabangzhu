"""补齐两类内容：按方法解析、逐行代码解析；并可补生成指定语言答案。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from ..prompts import loader as prompts
from ..store.db import default_db_path
from . import judge
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


def enrich_problem(service: Any, problem: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    pid = int(problem["id"])
    solutions = _solutions(pid)
    methods = _methods(solutions)
    if not methods:
        return {"ok": False, "reason": "没有已验证的解"}

    base = user_blocks(statement_excerpt(problem), spec.get("samples") or []) + "\n\n## 已有解法\n" + json.dumps(
        methods, ensure_ascii=False
    ) + "\n\n## 约束\n" + spec_block(spec)
    result = service._chat(
        prompts.METHOD_ANALYSIS, base, tag=f"method_analysis#{pid}", max_tokens=4096, json_mode=True,
    )
    payload = result.json_obj or {}
    got = payload.get("methods") or []
    by_name = {str(m.get("name") or ""): m for m in got if isinstance(m, dict)}
    approaches = []
    for method in methods:
        item = by_name.get(method["name"]) or {}
        approaches.append({
            "name": method["name"],
            "idea": str(item.get("idea") or "").strip(),
            "correctness": str(item.get("correctness") or "").strip(),
            "complexity": str(item.get("complexity") or method["complexity"]).strip(),
            "pitfalls": [str(x) for x in (item.get("pitfalls") or [])][:3],
        })

    with _db() as conn:
        row = conn.execute("SELECT analysis_json FROM problems WHERE id=?", (pid,)).fetchone()
        analysis = json.loads(row["analysis_json"]) if row and row["analysis_json"] else {}
        analysis["approaches"] = approaches
        conn.execute("UPDATE problems SET analysis_json=? WHERE id=?", (json.dumps(analysis, ensure_ascii=False), pid))

    notes_done = 0
    for sol in solutions:
        if sol["line_notes_json"]:
            continue
        body = (
            user_blocks(statement_excerpt(problem), spec.get("samples") or [])
            + f"\n\n## 方法\n{ {'name': sol['approach'] or '未标注方法', 'complexity': sol['complexity'] or '未声明'} }\n"
            + f"\n## 目标语言\n{sol['lang']}\n\n## 代码\n```{sol['lang']}\n{sol['code'][:12000]}\n```"
        )
        res = service._chat(
            prompts.CODE_NOTES, body,
            tag=f"code_notes#{pid}_{sol['lang']}_{sol['role']}", max_tokens=4096, json_mode=True,
        )
        data = res.json_obj or {}
        lines = [str(data.get("summary") or "").strip()]
        for i, step in enumerate(data.get("steps") or [], start=1):
            if isinstance(step, dict):
                lines.append(f"{i}. {step.get('part','')}：{step.get('explain','')}")
        pitfalls = data.get("pitfalls") or []
        if pitfalls:
            lines.append("易错点：" + "；".join(str(x) for x in pitfalls))
        text = "\n".join(x for x in lines if x)
        with _db() as conn:
            conn.execute(
                "UPDATE reference_solutions SET line_notes_json=? WHERE problem_id=? AND lang=? AND role=?",
                (json.dumps(text, ensure_ascii=False), pid, sol["lang"], sol["role"]),
            )
        notes_done += 1
    return {"ok": True, "methods": len(approaches), "notes": notes_done}


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
