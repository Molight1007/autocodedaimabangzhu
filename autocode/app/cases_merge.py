"""合并“我的数据”（契约 §4.2，领域层 / 确定性代码）。

流程（顺序不能换，每步都是门禁）：
1. **长度/格式校验**（在任何编译/执行之前）——入口安全；
2. 与既有 `input_data` **去重**（同层按 input_hash 唯一，跨层也按原文去重）；
3. 用**全部已验证参考解**各跑一遍；
4. 参考解之间一致 && 与用户给的 output 一致 → 入库（`source='user'`）；
   不一致 → 进 `rejected` 明细；
5. `force=true` 才跳过第 3/4 步直接入库（信任级别降为 reference_only）。

信任口径：`source_of_truth='hand'`（用户手工提供）；校验通过的 `trust_level='cross_verified'`，
force 入库的 `trust_level='reference_only'`，绝不冒充 oracle。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..store.db import Database, get_db

USER_LAYER = "user"
MAX_MERGE_CASES = 200
MAX_INPUT_CHARS = 20_000
MAX_OUTPUT_CHARS = 20_000
MERGE_TIME_LIMIT_MS = 5_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_cases(payload: Dict[str, Any], *, db: Optional[Database] = None) -> Dict[str, Any]:
    """入口：契约 §4.2 的 `POST /api/problems/{id}/cases`。"""
    database = db or get_db()
    problem_id = int(payload.get("problem_id") or payload.get("id") or 0)
    problem = database.query_one("SELECT id, spec_json FROM problems WHERE id=?", (problem_id,))
    if problem is None:
        return {"error": "题目不存在"}
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        return {"error": "cases 不能为空"}
    if len(raw_cases) > MAX_MERGE_CASES:
        return {"error": f"一次最多合并 {MAX_MERGE_CASES} 条用例"}
    force = bool(payload.get("force"))
    layer = str(payload.get("layer") or USER_LAYER)
    # S5：只允许把用户数据写进 user 层，不允许借合并接口污染 boundary/stress
    if layer != USER_LAYER:
        return {"error": f"layer 只允许 \"{USER_LAYER}\"，收到 {layer!r}"}
    spec = _jsonable(problem["spec_json"]) or {}
    checker = str(spec.get("checker_type") or "exact")
    eps = float(spec.get("float_eps") or 1e-6)

    incoming, invalid = _validate(raw_cases)
    existing = {
        str(row["input_data"])
        for row in database.query_all(
            "SELECT input_data FROM test_cases WHERE problem_id=?", (problem_id,)
        )
    }
    skipped = 0
    candidates: List[Dict[str, Any]] = []
    for item in incoming:
        if item["input"] in existing:
            skipped += 1
            continue
        existing.add(item["input"])
        candidates.append(item)

    rejected: List[Dict[str, Any]] = list(invalid)
    if candidates and not force:
        accepted, rejections = _verify_candidates(database, candidates, problem_id, checker, eps)
        rejected.extend(rejections)
    else:
        accepted = candidates

    added = _insert(database, problem_id, layer, accepted, verified=not force)
    return {"added": added, "skipped": skipped, "rejected": rejected}


def _jsonable(value: Any) -> Any:
    import json

    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def _validate(raw_cases: Sequence[Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """长度/格式校验：不合格的进 rejected，且**不进入执行阶段**。"""
    incoming: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            invalid.append({"index": index, "reason": "不是对象"})
            continue
        input_text = str(item.get("input") or "")
        output_text = str(item.get("output") or item.get("expected") or "")
        if not input_text.strip():
            invalid.append({"index": index, "reason": "input 为空"})
            continue
        if not output_text.strip():
            # S1：空输出入库等于制造一条无法判题的数据，force 也不放行
            invalid.append({"index": index, "reason": "output 为空"})
            continue
        if len(input_text) > MAX_INPUT_CHARS or len(output_text) > MAX_OUTPUT_CHARS:
            invalid.append({"index": index, "reason": "输入或输出超过长度上限"})
            continue
        incoming.append({"index": index, "input": input_text, "output": output_text})
    return incoming, invalid


def _verify_candidates(
    db: Database, candidates: Sequence[Dict[str, Any]], problem_id: int, checker: str, eps: float
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """全部已验证参考解跑一遍；一致才接受。"""
    from ..engine.verify.comparator import compare
    from . import judge

    solutions = db.query_all(
        "SELECT lang, code FROM reference_solutions WHERE problem_id=? AND verified=1", (problem_id,)
    )
    if not solutions:
        return [], [
            {"index": item["index"], "reason": "没有已验证参考解，无法校验（可用 force 强制入库）"}
            for item in candidates
        ]
    runs = [{"id": str(item["index"]), "input": item["input"]} for item in candidates]
    tables: List[Dict[str, str]] = []
    for solution in solutions:
        batch = judge.run_solution(
            solution["lang"],
            solution["code"],
            runs,
            checker_type=checker,
            float_eps=eps,
            time_limit_ms=MERGE_TIME_LIMIT_MS,
            compare_expected=False,
        )
        if batch.compile_error:
            continue
        tables.append(judge.batch_outputs(batch))
    if not tables:
        return [], [
            {"index": item["index"], "reason": "参考解全部无法运行", "expected": item["output"]}
            for item in candidates
        ]

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for item in candidates:
        case_id = str(item["index"])
        produced = [table[case_id] for table in tables if case_id in table]
        if len(produced) != len(tables):
            rejected.append(
                {"index": item["index"], "reason": "参考解未产出结果（超时或崩溃）", "expected": item["output"]}
            )
            continue
        baseline = produced[0]
        if any(not compare(checker, baseline, other, float_eps=eps).ok for other in produced[1:]):
            rejected.append(
                {
                    "index": item["index"],
                    "reason": "参考解之间不一致",
                    "expected": baseline,
                    "actual": produced[1],
                }
            )
            continue
        if not compare(checker, item["output"], baseline, float_eps=eps).ok:
            rejected.append(
                {
                    "index": item["index"],
                    "reason": "参考解不一致",
                    "expected": item["output"],
                    "actual": baseline,
                }
            )
            continue
        accepted.append({**item, "expected": baseline})
    return accepted, rejected


def _insert(
    db: Database, problem_id: int, layer: str, cases: Sequence[Dict[str, Any]], *, verified: bool
) -> int:
    if not cases:
        return 0
    trust = "cross_verified" if verified else "reference_only"
    now = _now()
    added = 0
    with db.transaction() as conn:
        # S4：MAX(ordinal) 必须在写事务内取；放外面会让并发 merge 拿到同一个基准
        row = conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM test_cases WHERE problem_id=? AND layer=?",
            (problem_id, layer),
        ).fetchone()
        base_ordinal = int(row[0] or 0)
        for offset, item in enumerate(cases, start=1):
            digest = hashlib.sha256(item["input"].encode("utf-8")).hexdigest()[:32]
            cursor = conn.execute(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " expected_output, source_of_truth, trust_level, size_class, verified_at, source)"
                " VALUES(?, ?, ?, ?, ?, ?, 'hand', ?, ?, ?, 'user')"
                " ON CONFLICT(problem_id, layer, input_hash) DO NOTHING",
                (
                    problem_id,
                    layer,
                    base_ordinal + offset,
                    item["input"],
                    digest,
                    item.get("expected") or item.get("output") or "",
                    trust,
                    layer,
                    now,
                ),
            )
            added += max(0, int(cursor.rowcount or 0))
    return added
