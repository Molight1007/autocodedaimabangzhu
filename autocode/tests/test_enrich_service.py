"""enrich_service 的库隔离测试（阻断项：曾硬编码 data/autocode.db 绕过 AUTOCODE_DB）。

验收口径（team-lead）：
- `AUTOCODE_DB` 指向临时库时，`add_language` / `enrich_problem` 必须写进临时库；
- 真实 `data/autocode.db` 行数前后不变；
- 源码里不允许再出现硬编码库路径。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autocode.app import enrich_service
from autocode.app.prep_models import RefArtifact
from autocode.store.db import Database, project_root, reset_default_db

REAL_DB = project_root() / "data" / "autocode.db"


def _counts(path: Path) -> dict:
    """只读统计真实库行数（文件不存在时返回空）。"""
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("problems", "reference_solutions", "hints")
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


class FakeService:
    """替代 PrepService：只提供 enrich_service 用到的两个方法，不触网。"""

    def __init__(self, chat_payload: dict | None = None) -> None:
        self.chat_payload = chat_payload or {}
        self.tags: list[str] = []

    def _chat(self, template, body, *, tag="", max_tokens=0, json_mode=True):
        self.tags.append(tag)
        return SimpleNamespace(json_obj=self.chat_payload)

    def _build_solution(self, problem, spec, samples, *, lang, role, template, cases, attempts):
        return RefArtifact(
            lang=lang,
            role=role,
            code="import sys\nprint(sys.stdin.read().strip())\n",
            verified=1,
            verification_level="cross_verified",
            approach="模拟法",
            complexity="O(1)",
            samples_passed=1,
            samples_total=1,
        )


class EnrichCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "temp.db"
        self.env = patch.dict(os.environ, {"AUTOCODE_DB": str(self.db_path)}, clear=False)
        self.env.start()
        reset_default_db()
        db = Database(self.db_path)
        db.init_schema()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(id, title, statement_md, spec_json, status, created_at, updated_at)"
                " VALUES(1, '隔离题', '题面', ?, 'prepared', 'now', 'now')",
                (json.dumps({"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}),),
            )
            conn.execute(
                "INSERT INTO reference_solutions(problem_id, lang, role, code, verified, verification_level,"
                " approach, complexity, is_primary, samples_expected, created_at)"
                " VALUES(1, 'python', 'reference', 'import sys\nprint(sys.stdin.read().strip())\n', 1,"
                " 'cross_verified', '模拟法', 'O(1)', 1, 1, 'now')"
            )
        db.close()
        self.spec = {"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}
        self.problem = {"id": 1, "statement_md": "题面"}

    def tearDown(self) -> None:
        reset_default_db()
        self.env.stop()
        self._tmp.cleanup()

    def _temp_rows(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()


class TestConnectionIsolation(EnrichCase):
    def test_connects_to_env_database(self) -> None:
        with enrich_service._db() as conn:
            path = conn.execute("PRAGMA database_list").fetchone()[2]
        self.assertEqual(Path(path).resolve(), self.db_path.resolve())

    def test_no_hardcoded_path_in_source(self) -> None:
        """回归护栏：源码里不能再出现 `ROOT / "data"` 这种硬编码。"""
        source = Path(enrich_service.__file__).read_text(encoding="utf-8")
        self.assertIn("default_db_path", source)
        self.assertNotIn('ROOT / "data"', source)
        self.assertNotIn("DB_PATH", source)

    def test_add_language_writes_temp_not_real(self) -> None:
        real_before = _counts(REAL_DB)
        service = FakeService()
        result = enrich_service.add_language(service, self.problem, self.spec, "c")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["lang"], "c")

        rows = self._temp_rows(
            "SELECT lang, verified, approach FROM reference_solutions WHERE problem_id=1 AND lang='c'"
        )
        self.assertEqual(len(rows), 1, "C 解应写进临时库")
        self.assertEqual(int(rows[0][1]), 1)
        self.assertEqual(rows[0][2], "模拟法")
        self.assertEqual(_counts(REAL_DB), real_before, "真实库行数必须不变")

    def test_enrich_problem_writes_temp_not_real(self) -> None:
        real_before = _counts(REAL_DB)
        payload = {
            "methods": [{"name": "模拟法", "idea": "直接模拟", "correctness": "正确", "complexity": "O(1)", "pitfalls": ["边界"]}]
        }
        service = FakeService(chat_payload=payload)
        result = enrich_service.enrich_problem(service, self.problem, self.spec)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["methods"], 1)

        rows = self._temp_rows("SELECT analysis_json FROM problems WHERE id=1")
        analysis = json.loads(rows[0][0])
        self.assertEqual(len(analysis["approaches"]), 1)
        self.assertEqual(analysis["approaches"][0]["name"], "模拟法")
        self.assertEqual(_counts(REAL_DB), real_before, "真实库行数必须不变")

    def test_judge_all_reads_temp(self) -> None:
        ok = enrich_service.judge_all(1, self.spec)
        self.assertTrue(ok, "回放样例应通过（临时库里的解能跑）")


if __name__ == "__main__":
    unittest.main()
