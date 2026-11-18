"""题目包导出与版本历史（契约 §4.3，领域层 / 确定性代码）。

**并发安全（B2，审查阻断项）**：旧实现的 `next_version()` 在事务外、写文件与写历史分开，
6 线程并发会拿到重复版本号、互相覆盖文件、甚至 `PermissionError`（同一路径同时写）。
现在改成：
1. `BEGIN IMMEDIATE` 内先算版本号并**插入历史行占号**（唯一索引 `(problem_id, version)` 兜底）；
2. 文件先写 `*.tmp` 再 `os.replace()`（原子替换，避免读到半个 zip）；
3. 任何一步失败 → 事务回滚、临时文件清理，不留下"有历史没文件"或"有文件没历史"。

**目录隔离**：`export_dir()` 优先 `AUTOCODE_EXPORT_DIR`；否则**非默认库**（如测试/副本库
`.tmp/xxx.db`）落到 `db.path.parent/exports`，默认库仍落仓库 `exports/`。
这样副本库跑批不会覆盖真实导出文件（审查发现的越界写入）。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..store.db import Database, default_db_path, get_db, project_root

HISTORY_LIMIT = 10
EXT_MAP = {"python": "py", "cpp": "cpp", "c": "c"}
TMP_SUFFIX = ".tmp"


def export_dir(db: Optional[Database] = None) -> Path:
    """导出目录：环境变量 > 副本库同目录 > 仓库默认 `exports/`。"""
    override = os.environ.get("AUTOCODE_EXPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    database = db or get_db()
    db_path = Path(database.path).expanduser()
    try:
        if db_path.resolve() == Path(default_db_path()).expanduser().resolve():
            return project_root() / "exports"
    except OSError:
        pass
    return db_path.parent / "exports"


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename_part(text: str) -> str:
    """Windows 文件名非法字符清洗（含路径分隔符），并限制长度。"""
    keep = "".join(ch for ch in str(text or "") if ch not in '\\/:*?"<>|').strip()
    return (keep or "problem")[:40]


def next_version(db: Database, problem_id: int) -> int:
    """只读预览用（不要拿它去写库；真正的版本号在事务内分配）。"""
    current = db.query_scalar(
        "SELECT COALESCE(MAX(version), 0) FROM export_history WHERE problem_id=?", (problem_id,), 0
    )
    return int(current or 0) + 1


def export_problem(problem_id: int, *, db: Optional[Database] = None) -> Dict[str, Any]:
    """打 zip 包并写历史；返回 `{ok,path,filename,bytes,cases,version}`。"""
    database = db or get_db()
    problem = database.query_one("SELECT * FROM problems WHERE id=?", (problem_id,))
    if problem is None:
        return {"error": "题目不存在"}
    cases = database.query_all(
        "SELECT id, layer, ordinal, input_data, expected_output, trust_level, source"
        " FROM test_cases WHERE problem_id=? ORDER BY ordinal, id",
        (problem_id,),
    )
    solutions = database.query_all(
        "SELECT lang, role, approach, complexity, verified, code, line_notes_json"
        " FROM reference_solutions WHERE problem_id=?",
        (problem_id,),
    )
    hints = database.query_all(
        "SELECT level, content FROM hints WHERE problem_id=? ORDER BY level", (problem_id,)
    )
    spec = _jsonable(problem["spec_json"]) or {}
    analysis = _jsonable(problem["analysis_json"]) or {}
    title_part = safe_filename_part(problem["title"])
    target_dir = export_dir(database)

    tmp_path: Optional[Path] = None
    target: Optional[Path] = None
    data = b""
    filename = ""
    version = 0
    history_id = 0

    # 事务 1（短）：只占版本号 + 插历史行，立刻提交释放写锁
    try:
        with database.transaction() as conn:
            version = _reserve_version(conn, problem_id)
            filename = f"{problem_id}-{title_part}-v{version}.zip"
            cursor = conn.execute(
                "INSERT INTO export_history(problem_id, version, filename, bytes, cases, created_at)"
                " VALUES(?, ?, ?, 0, ?, ?)",
                (problem_id, version, filename, len(cases), _now()),
            )
            history_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:  # 版本号撞车（理论上被 BEGIN IMMEDIATE 挡住）
        return {"error": f"导出版本冲突，请重试：{exc}"}

    # 事务外打包 + 原子替换：807 条用例的包会打包几十毫秒到几百毫秒，
    # 放在写事务里会长时间占锁，把其它请求的写卡在 busy_timeout 上。
    try:
        data = _build_zip(problem, spec, list(cases), list(solutions), list(hints), analysis, version)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        tmp_path = target_dir / (filename + TMP_SUFFIX)
        tmp_path.write_bytes(data)
        os.replace(tmp_path, target)
        tmp_path = None
    except Exception as exc:  # noqa: BLE001 - 打包/落盘任何失败都要补偿掉历史行
        _cleanup_tmp(tmp_path)
        _drop_history_row(database, history_id)
        return {"error": f"写导出文件失败：{exc}"}

    # 事务 2（短）：回填字节数。失败不影响产物（历史行 bytes=0 可接受）
    try:
        with database.transaction() as conn:
            conn.execute("UPDATE export_history SET bytes=? WHERE id=?", (len(data), history_id))
    except sqlite3.Error:
        pass

    return {
        "ok": True,
        "path": str(target),
        "filename": filename,
        "bytes": len(data),
        "cases": len(cases),
        "version": version,
    }


def _drop_history_row(db: Database, history_id: int) -> None:
    """补偿：打包失败时删掉刚占位的历史行，避免"有历史没文件"。"""
    if not history_id:
        return
    try:
        with db.transaction() as conn:
            conn.execute("DELETE FROM export_history WHERE id=?", (history_id,))
    except sqlite3.Error:
        pass


def _reserve_version(conn: Any, problem_id: int) -> int:
    """在写锁内取版本号（`BEGIN IMMEDIATE` 已持有，读取即独占）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM export_history WHERE problem_id=?", (problem_id,)
    ).fetchone()
    return int(row[0] or 0) + 1


def _cleanup_tmp(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _build_zip(
    problem: Any,
    spec: Dict[str, Any],
    cases: List[Any],
    solutions: List[Any],
    hints: List[Any],
    analysis: Dict[str, Any],
    version: int,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.md", problem["statement_md"] or "")
        archive.writestr("meta.json", json.dumps(_meta(problem, spec, solutions, version), ensure_ascii=False, indent=2))
        archive.writestr("cases.json", json.dumps(_cases_payload(cases), ensure_ascii=False, indent=2))
        archive.writestr("analysis.json", json.dumps(analysis, ensure_ascii=False, indent=2))
        archive.writestr(
            "hints.json",
            json.dumps(
                [{"level": h["level"], "content": h["content"]} for h in hints],
                ensure_ascii=False,
                indent=2,
            ),
        )
        for index, solution in enumerate(solutions, 1):
            name = (
                f"solutions/{solution['lang']}_{solution['role']}_{index}."
                f"{EXT_MAP.get(solution['lang'], 'txt')}"
            )
            archive.writestr(name, solution["code"])
        # OJ 通用的 1.in/1.ans 文件对（统一 LF：库里历史数据可能带 Windows CRLF）
        for case_no, row in enumerate(cases, 1):
            input_text = str(row["input_data"] or "").replace("\r\n", "\n")
            output_text = str(row["expected_output"] or "").replace("\r\n", "\n")
            if not input_text.endswith("\n"):
                input_text += "\n"
            if not output_text.endswith("\n"):
                output_text += "\n"
            archive.writestr(f"cases/{case_no}.in", input_text)
            archive.writestr(f"cases/{case_no}.ans", output_text)
        archive.writestr("solutions.json", json.dumps(_solutions_payload(solutions), ensure_ascii=False, indent=2))
    return buffer.getvalue()


def _meta(problem: Any, spec: Dict[str, Any], solutions: List[Any], version: int) -> Dict[str, Any]:
    return {
        "id": problem["id"],
        "title": problem["title"],
        "tier": problem["tier"],
        "status": problem["status"],
        "source": problem["source"],
        "version": version,
        "tags": _jsonable(problem["algorithm_tags"]) or [],
        "checker_type": spec.get("checker_type", "exact"),
        "langs": sorted({s["lang"] for s in solutions}),
        "samples": spec.get("samples") or [],
        # 真实 spec 里的键是 constraints_exec（旧实现读 constraints 导致恒为空）
        "constraints": spec.get("constraints_exec") or [],
    }


def _cases_payload(cases: List[Any]) -> List[Dict[str, Any]]:
    """`id` 用数据库主键（跨层唯一）；旧实现用 ordinal，跨层会重号。"""
    return [
        {
            "id": f"case{r['id']}",
            "layer": r["layer"],
            "ordinal": r["ordinal"],
            "input": r["input_data"],
            "output": r["expected_output"],
            "trust_level": r["trust_level"],
            "source": r["source"],
        }
        for r in cases
    ]


def _solutions_payload(solutions: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "lang": s["lang"],
            "role": s["role"],
            "approach": s["approach"],
            "complexity": s["complexity"],
            "verified": bool(s["verified"]),
            "line_notes": _jsonable(s["line_notes_json"]) if s["line_notes_json"] else "",
        }
        for s in solutions
    ]


def list_exports(problem_id: int, *, db: Optional[Database] = None) -> List[Dict[str, Any]]:
    """最近 10 次导出（版本/文件名/大小/用例数/时间）。"""
    database = db or get_db()
    rows = database.query_all(
        "SELECT version, filename, bytes, cases, created_at FROM export_history"
        " WHERE problem_id=? ORDER BY id DESC LIMIT ?",
        (problem_id, HISTORY_LIMIT),
    )
    return [dict(row) for row in rows]
