"""题库读取：列表、详情、测试数据分页。

从 api.py 拆出来，理由与 `api_feedback.py` 相同：api.py 曾经被顶过 800 行硬线。
这里只读不写，权限判断（能不能增删）不在这里——写操作仍在 api.py 与 segment.py。
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import banks, jobs, segment
from .api_common import _loads, _page_limit


def _contract_view(raw: Any) -> Dict[str, Any]:
    """库里存的契约 → 界面要的形态。

    `summary` 在这里拼好：前端不该知道 DSL 的结构，否则契约一变就要改两处。
    """
    from . import io_spec

    contract = io_spec.loads(raw)
    if not contract:
        return {}
    return {
        "summary": io_spec.describe(contract["io"]),
        "judge_type": str(contract.get("judge_type") or io_spec.judge_of(contract["io"])),
        "samples": contract.get("samples") or [],
        "notes": str(contract.get("notes") or ""),
    }


def list_problems(
    db: Any,
    *,
    q: str = "",
    status: str = "",
    offset: int = 0,
    limit: int = 50,
    bank_id: int = banks.DEFAULT_ID,
    role: str = "student",
) -> Dict[str, Any]:
    bank = banks.get(db, bank_id)
    if bank is None:
        raise KeyError(bank_id)
    where = ["bank_id=?"]
    params: List[Any] = [int(bank["id"])]
    if q.strip():
        where.append("title LIKE ?")
        params.append(f"%{q.strip()}%")
    if status.strip():
        where.append("status=?")
        params.append(status.strip())
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM problems WHERE {clause}", tuple(params), 0)
    rows = db.query_all(
        f"SELECT id, title, status, dataset_version, samples_json, difficulty, created_at"
        f" FROM problems WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params + [_page_limit(limit), max(0, int(offset))]),
    )
    items = []
    for row in rows:
        pid = int(row["id"])
        items.append(
            {
                "id": pid,
                "title": str(row["title"]),
                "status": str(row["status"]),
                "version": int(row["dataset_version"] or 0),
                "samples": len(_loads(row["samples_json"], [])),
                "difficulty": str(row["difficulty"] or ""),
                # 只统计当前版本：历史版本仍留在库里（便于回溯），但界面上的
                # "数据 N 条"必须等于详情页看到的条数，否则会以为数据凭空变多
                "cases": int(db.query_scalar(
                    "SELECT COUNT(*) FROM test_cases"
                    " WHERE problem_id=? AND version=? AND status='active'",
                    (pid, int(row["dataset_version"] or 0)), 0,
                )),
                "adversarial": int(db.query_scalar(
                    "SELECT COUNT(*) FROM adversarial_cases WHERE problem_id=? AND status='active'",
                    (pid,), 0,
                )),
                "created_at": str(row["created_at"] or ""),
            }
        )
    return {
        "ok": True,
        "total": int(total or 0),
        "cap": banks.cap_of(bank),
        "bank": banks.view(db, bank, role),
        "items": items,
    }


def _problem_figures(db: Any, problem_id: int) -> List[Dict[str, Any]]:
    """题目插图（老库没有该表时返回空，不让详情页炸掉）。"""
    try:
        rows = db.query_all(
            "SELECT label, page, bbox_json, path, width, height, after_line"
            " FROM problem_figures WHERE problem_id=? ORDER BY ordinal, id",
            (int(problem_id),),
        )
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "label": str(item["label"]),
            "page": int(item["page"] or 0),
            "bbox": _loads(item["bbox_json"], []),
            "url": str(item["path"] or ""),
            "width": float(item["width"] or 0),
            "height": float(item["height"] or 0),
            "after_line": int(item["after_line"] or 1),
        }
        for item in rows
    ]


def problem_detail(db: Any, problem_id: int, role: str = "student") -> Dict[str, Any]:
    row = db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
    if row is None:
        raise KeyError(problem_id)
    pid = int(row["id"])
    # 题目属于哪个库：界面据此决定「删除」是否可点（永久库只有管理员能删）
    bank = banks.get(db, row["bank_id"])
    if bank is None:
        bank = {"id": banks.DEFAULT_ID, "name": banks.DEFAULT_NAME, "kind": banks.KIND_TEMP}
    version = int(row["dataset_version"] or 0)
    layers = {
        str(item["layer"]): int(item["c"])
        for item in db.query_all(
            "SELECT layer, COUNT(*) AS c FROM test_cases"
            " WHERE problem_id=? AND version=? AND status='active' GROUP BY layer",
            (pid, version),
        )
    }
    trusts = {
        str(item["trust"]): int(item["c"])
        for item in db.query_all(
            "SELECT trust, COUNT(*) AS c FROM test_cases"
            " WHERE problem_id=? AND version=? AND status='active' GROUP BY trust",
            (pid, version),
        )
    }
    # 老题的样例是导入那一刻算的：解析规则升级后仍是 0 组。详情页顺手补算一次，
    # 否则界面会一直显示"样例 0 组"，用户根本分不清是自己题面没写样例还是解析没认出来。
    samples = _loads(row["samples_json"], [])
    if not samples:
        samples = segment.refresh_samples(db, row)
    return {
        "ok": True,
        "id": pid,
        "title": str(row["title"]),
        "bank": banks.view(db, bank, role),
        "statement": str(row["statement_md"]),
        # 题面补全：界面要能说清"这段是模型补的"，并让用户看到原文
        "statement_repaired": bool(row["statement_repaired"]),
        "statement_original": str(row["statement_original"] or ""),
        "figures": _problem_figures(db, pid),
        # 排版分块：模型只给行号与类型，位置由前端按块拼；空数组表示用自动排版
        "layout": _loads(row["layout_json"], []),
        "samples": samples,
        # 已确定的 I/O 契约（程序按什么格式读、判定方式是什么）。
        # 空对象表示还没生成过；界面据此提示"这份数据按什么格式喂程序"。
        "exec_spec": _contract_view(row["exec_spec_json"]),
        "constraints": _loads(row["constraints_json"], []),
        "difficulty": str(row["difficulty"] or ""),
        "judge_type": str(row["judge_type"]),
        "tolerance": str(row["tolerance"] or ""),
        "multi_testcase": bool(row["multi_testcase"]),
        "status": str(row["status"]),
        "version": version,
        "quality": _loads(row["quality_json"], {}),
        "layers": layers,
        "trusts": trusts,
        "total_cases": sum(layers.values()),
        "jobs": {
            "dataset": jobs.status(db, pid, "dataset"),
            "adversarial": jobs.status(db, pid, "adversarial"),
        },
        "created_at": str(row["created_at"] or ""),
    }


def list_cases(
    db: Any,
    problem_id: int,
    *,
    layer: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    row = db.query_one("SELECT dataset_version FROM problems WHERE id=?", (int(problem_id),))
    if row is None:
        raise KeyError(problem_id)
    version = int(row["dataset_version"] or 0)
    where = ["problem_id=?", "version=?", "status='active'"]
    params: List[Any] = [int(problem_id), version]
    if layer.strip():
        where.append("layer=?")
        params.append(layer.strip())
    clause = " AND ".join(where)
    total = db.query_scalar(f"SELECT COUNT(*) FROM test_cases WHERE {clause}", tuple(params), 0)
    rows = db.query_all(
        f"SELECT id, layer, size_class, input_data, expected_output, trust, source, ordinal"
        f" FROM test_cases WHERE {clause} ORDER BY ordinal, id LIMIT ? OFFSET ?",
        tuple(params + [_page_limit(limit), max(0, int(offset))]),
    )
    return {
        "ok": True,
        "version": version,
        "total": int(total or 0),
        "items": [
            {
                "id": int(item["id"]),
                "layer": str(item["layer"]),
                "size_class": str(item["size_class"] or ""),
                "input": str(item["input_data"]),
                "expected_output": str(item["expected_output"]),
                "trust": str(item["trust"] or ""),
                "source": str(item["source"] or ""),
                "ordinal": int(item["ordinal"] or 0),
            }
            for item in rows
        ],
    }
