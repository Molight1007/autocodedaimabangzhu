"""题目级管理操作：删除整题、人工注入 spec（契约 §1 领域层 / 确定性代码）。

为什么单独成模块：删除要跨 11 张表对账（FK 级联只覆盖一半），spec 注入要走
"归一化 + 样例回放"同一道门——两处调用点（CLI、web/api）共用一份实现，
避免两份删除清单漂移（漏一张表就是脏数据）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..store.db import Database, get_db
from .prep_rules import normalize_spec, replay_samples
from .prep_store import PrepStore

#: 无 FK 级联、需要手工清理的子表（schema.sql 里没有 REFERENCES problems 的）
_ORPHAN_TABLES = (
    "problem_stage_runs",
    "problem_progress",
    "problem_drafts",
    "export_history",
    "generation_jobs",
)


def delete_problem(problem_id: int, *, db: Optional[Database] = None) -> Dict[str, Any]:
    """删除一道题及其全部产物。返回 {ok, title, removed} 或 {error}。

    顺序：study_records 是 RESTRICT（练习记录阻止删题），先显式删它
    （run_results 随之级联）；再删 problems 让 FK 级联清掉
    test_cases/reference_solutions/generators/hints/similar_problems/llm_calls；
    最后清没有 FK 的对账表。
    """
    database = db or get_db()
    row = database.query_one("SELECT id, title, status FROM problems WHERE id=?", (problem_id,))
    if row is None:
        return {"error": f"题目 {problem_id} 不存在"}
    if row["status"] == "preparing":
        return {"error": "题目正在生成中，先等任务结束（或在任务列表里终止）再删除"}
    removed: Dict[str, int] = {}
    with database.transaction() as conn:
        # generation_jobs 里若有 running/queued 记录（历史残留），先落 failed，防止对账时复活；
        # 排队中的任务还没起进程，改状态即可，下面整表删除会一并带走
        conn.execute(
            "UPDATE generation_jobs SET status='failed', error='题目已删除',"
            " finished_at=datetime('now') WHERE problem_id=? AND status IN ('running','queued')",
            (problem_id,),
        )
        removed["study_records"] = conn.execute(
            "DELETE FROM study_records WHERE problem_id=?", (problem_id,)
        ).rowcount
        conn.execute("DELETE FROM problems WHERE id=?", (problem_id,))
        for table in _ORPHAN_TABLES:
            removed[table] = conn.execute(
                f"DELETE FROM {table} WHERE problem_id=?", (problem_id,)
            ).rowcount
    return {"ok": True, "id": problem_id, "title": row["title"], "removed": removed}


def set_spec(problem_id: int, spec_raw: Dict[str, Any], *, db: Optional[Database] = None) -> Dict[str, Any]:
    """人工注入 spec：过与流水线相同的门（normalize + 样例回放），全过才落库。

    用途：spec 阶段连续失败的题（变体输入、怪格式），由人/外部工具写好 spec JSON
    后直接接管，后续 prepare 的 `_stage_spec_reuse` 会自动复用、跳过 LLM 生成。
    """
    database = db or get_db()
    store = PrepStore(database)
    problem = store.load_problem(problem_id)
    if problem is None:
        return {"error": f"题目 {problem_id} 不存在"}
    samples = store.load_samples(problem_id)
    if not samples:
        return {"error": "题目没有可用样例，无法验证 spec（先重新导入题面）"}
    spec, problems = normalize_spec(spec_raw)
    if problems:
        return {"error": "spec 不合格：" + "；".join(problems)}
    errors = replay_samples(spec, samples)
    if errors:
        return {"error": "spec 样例回放失败：" + "；".join(errors[:3])}
    spec["samples"] = samples
    store.save_spec(problem_id, spec)
    # 状态复位：failed/draft 统一回 draft，让 generate 愿意再跑（且 spec 阶段会复用）
    store.update_problem(problem_id, status="draft", failed_stage=None)
    return {
        "ok": True,
        "id": problem_id,
        "grammar_nodes": len(spec.get("input_grammar") or []),
        "samples_replayed": len(samples),
    }


def load_spec_file(text: str) -> Dict[str, Any]:
    """解析人工 spec 文本（允许 ```json 围栏包裹）。"""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"spec 文件不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("spec 文件必须是一个 JSON 对象")
    return payload
