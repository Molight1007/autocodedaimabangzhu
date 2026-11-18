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


class ScriptedService(FakeService):
    """按调用顺序返回不同 payload；用于"先缺后补"/"一直缺"的重试测试。"""

    def __init__(self, payloads: list[dict]) -> None:
        super().__init__(payloads[-1] if payloads else {})
        self.payloads = list(payloads)

    def _chat(self, template, body, *, tag="", max_tokens=0, json_mode=True):
        self.tags.append(tag)
        index = min(self.tags.count(tag) - 1, len(self.payloads) - 1) if self.payloads else 0
        payload = self.payloads[index] if self.payloads else {}
        return SimpleNamespace(json_obj=payload)

    def method_calls(self) -> int:
        return sum(1 for tag in self.tags if str(tag).startswith("method_analysis"))


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


class TestMethodRetry(EnrichCase):
    """按方法解析的静默失败修复：缺字段要重试，重试用尽要标记，不能伪造内容。"""

    GOOD = {"name": "模拟法", "idea": "按题意逐个时间点推演", "correctness": "每一步都在合法状态上",
            "complexity": "O(1)", "pitfalls": ["边界"]}

    def _approaches(self) -> list[dict]:
        rows = self._temp_rows("SELECT analysis_json FROM problems WHERE id=1")
        return json.loads(rows[0][0])["approaches"]

    def test_retry_fills_missing_field(self) -> None:
        first = {"methods": [{"name": "模拟法", "idea": "", "correctness": "", "complexity": "O(1)"}]}
        second = {"methods": [self.GOOD]}
        service = ScriptedService([first, second])
        result = enrich_service.enrich_problem(service, self.problem, self.spec)

        self.assertTrue(result["ok"], result)
        self.assertEqual(service.method_calls(), 2, "第一次缺字段 → 必须带反馈重试一次")
        self.assertEqual(result["flagged"], [])
        approach = self._approaches()[0]
        self.assertEqual(approach["idea"], self.GOOD["idea"])
        self.assertEqual(approach["correctness"], self.GOOD["correctness"])

    def test_always_missing_flags_instead_of_faking(self) -> None:
        empty = {"methods": [{"name": "模拟法", "idea": "", "correctness": "", "complexity": "O(1)"}]}
        service = ScriptedService([empty])
        result = enrich_service.enrich_problem(service, self.problem, self.spec)

        self.assertTrue(result["ok"], result)
        self.assertEqual(service.method_calls(), enrich_service.MAX_METHOD_ATTEMPTS)
        self.assertEqual(result["flagged"], ["模拟法"])
        approach = self._approaches()[0]
        # 缺就是缺：不编造、不写"正确"之类的兜底文案
        self.assertEqual(approach["idea"], "")
        self.assertEqual(approach["correctness"], "")

        rows = self._temp_rows("SELECT analysis_json FROM problems WHERE id=1")
        analysis = json.loads(rows[0][0])
        # 口径（与前端约定）：quality_flags 每项都是自解释人话，不写裸方法名
        self.assertIn("方法「模拟法」解析缺失", analysis["quality_flags"])
        self.assertNotIn("模拟法", analysis["quality_flags"], "裸方法名前端渲染出来看不懂")
        self.assertEqual(result["flagged"], ["模拟法"], "flagged 保留结构化裸名供脚本消费")

    def test_quality_flags_merged_and_deduped(self) -> None:
        """已有人话条目（如 explain() 写的"题解包含代码块"）要保留，且不重复追加。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE problems SET analysis_json=? WHERE id=1",
                (json.dumps({"quality_flags": ["题解包含代码块", "方法「模拟法」解析缺失"]},
                            ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()
        empty = {"methods": [{"name": "模拟法", "idea": "", "correctness": "", "complexity": "O(1)"}]}
        enrich_service.enrich_problem(ScriptedService([empty]), self.problem, self.spec)

        rows = self._temp_rows("SELECT analysis_json FROM problems WHERE id=1")
        flags = json.loads(rows[0][0])["quality_flags"]
        self.assertEqual(flags, ["题解包含代码块", "方法「模拟法」解析缺失"], "合并去重、不追加重复项")

    def test_existing_non_empty_field_not_overwritten(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE problems SET analysis_json=? WHERE id=1",
                (json.dumps({"approaches": [{
                    "name": "模拟法", "idea": "旧解析", "correctness": "旧正确性",
                    "complexity": "O(n log n)", "pitfalls": ["旧坑A", "旧坑B"],
                }]}, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()
        # 模型这次只给了 name/idea/correctness：complexity 为空、pitfalls 缺失
        payload = {"methods": [{"name": "模拟法", "idea": "", "correctness": "", "complexity": ""}]}
        result = enrich_service.enrich_problem(ScriptedService([payload]), self.problem, self.spec)
        self.assertEqual(result["flagged"], ["模拟法"])
        approach = self._approaches()[0]
        self.assertEqual(approach["idea"], "旧解析", "不得覆盖已有的非空字段")
        self.assertEqual(approach["correctness"], "旧正确性")
        # 回归（审查 B1）：complexity 不能被方法表的兜底值“未声明”顶掉，pitfalls 不能被清空
        self.assertEqual(approach["complexity"], "O(n log n)")
        self.assertEqual(approach["pitfalls"], ["旧坑A", "旧坑B"])

    def test_stale_missing_flag_cleared_after_fields_filled(self) -> None:
        """标记有生命周期：字段补齐后旧的「方法缺失」标记必须消失，其它人话标记保留。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE problems SET analysis_json=? WHERE id=1",
                (json.dumps({"quality_flags": ["题解包含代码块", "方法「模拟法」解析缺失"]},
                            ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()
        good = {"methods": [{"name": "模拟法", "idea": "按题意推演", "correctness": "状态合法",
                             "complexity": "O(1)", "pitfalls": ["边界"]}]}
        result = enrich_service.enrich_problem(ScriptedService([good]), self.problem, self.spec)
        self.assertEqual(result["flagged"], [])
        rows = self._temp_rows("SELECT analysis_json FROM problems WHERE id=1")
        flags = json.loads(rows[0][0])["quality_flags"]
        self.assertNotIn("方法「模拟法」解析缺失", flags, "已补齐的旧标记必须清掉")
        self.assertIn("题解包含代码块", flags, "其它来源的人话标记不能被误删")


if __name__ == "__main__":
    unittest.main()
