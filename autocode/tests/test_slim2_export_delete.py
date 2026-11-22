"""导出与删除：数据要拿得走，导错的题要删得掉。

学生视角的两个死路（2026-11-21 试用）：
1. 数据生成完只能看，导不出，等于整个工具不闭环；
2. 导错一道题后删不掉（旧逻辑只允许删草稿），只能将错就错。
删除有数据的题因此加了"输入题目名确认"，这里同时验证子表被清干净
——SQLite 连接没开 `PRAGMA foreign_keys`，`ON DELETE CASCADE` 不会自动生效。
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from autocode.slim2 import api
from autocode.slim2 import config
from autocode.store.db import Database

NOW = "2026-11-21T00:00:00+00:00"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 直接建库，不走 slim2.db.get_db：后者会 apply_env() 改写全局
        # AUTOCODE_DB，把同一次 pytest 里后面的完整版测试带到错误的库上
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        self.pid = self._seed()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _seed(self, *, version: int = 1) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title,statement_md,statement_hash,samples_json,"
                " constraints_json,judge_type,status,dataset_version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("两数之和", "给定 a b，输出 a+b。", "hash-1",
                 json.dumps([{"input": "1 2", "output": "3"}]),
                 json.dumps(["1 <= a, b <= 10^9"]), "token", "ready", version, NOW, NOW),
            )
            pid = int(cursor.lastrowid)
            for index, (layer, data, output, trust) in enumerate([
                ("sample", "1 2", "3", "sample"),
                ("boundary", "1000000000 1000000000", "2000000000", "cross_verified"),
            ]):
                case = conn.execute(
                    "INSERT INTO test_cases(problem_id,version,layer,size_class,input_data,"
                    " input_hash,expected_output,trust,source,status,ordinal,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, version, layer, "tiny", data, f"h{index}", output, trust,
                     "sample" if layer == "sample" else "generated", "active", index, NOW),
                )
                if index == 0:
                    conn.execute(
                        "INSERT INTO case_reviews(case_id,reviewer_model,verdict,reason,"
                        " prompt_version,created_at) VALUES(?,?,?,?,?,?)",
                        (int(case.lastrowid), "fake", "pass", "", "v1", NOW),
                    )
            conn.execute(
                "INSERT INTO dataset_plans(problem_id,version,difficulty,layers_json,"
                " coverage_json,target_count,model,prompt_version,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, version, "easy", "{}", "[]", 12, "fake", "v2", NOW),
            )
            sub = conn.execute(
                "INSERT INTO submissions(problem_id,lang,code,code_hash,status,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, "python", "print(0)", "hc", "done", NOW),
            )
            sid = int(sub.lastrowid)
            adv = conn.execute(
                "INSERT INTO adversarial_cases(problem_id,submission_id,input_data,input_hash,"
                " expected_output,predicted_output,actual_output,run_verdict,targets,why,"
                " status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, sid, "0 0", "ha", "0", "0", "", "WA", "边界", "漏掉 0", "active", NOW),
            )
            conn.execute(
                "INSERT INTO adversarial_reviews(adv_case_id,reviewer_model,verdict,reason_code,"
                " reason,prompt_version,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(adv.lastrowid), "fake", "accept", "ok", "", "v1", NOW),
            )
            conn.execute(
                "INSERT INTO jobs(problem_id,kind,status,stage,budget,started_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, "dataset", "done", "", 8, NOW),
            )
        return pid

    def _count(self, table: str, where: str, params: tuple) -> int:
        return int(self.db.query_scalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", params, 0))


class TestExport(Base):
    def test_json_has_problem_dataset_and_adversarial(self) -> None:
        result = api.export_problem(self.db, self.pid, "json")
        self.assertTrue(result["ok"])
        payload = json.loads(result["content"])
        self.assertEqual(payload["problem"]["title"], "两数之和")
        self.assertEqual(payload["problem"]["version"], 1)
        self.assertEqual(len(payload["dataset"]), 2)
        self.assertEqual(payload["dataset"][1]["trust"], "cross_verified")
        self.assertEqual(len(payload["adversarial"]), 1)
        # 学生实际输出要带上，否则读的人不知道这条用例卡住了什么
        self.assertEqual(payload["adversarial"][0]["verdict"], "WA")
        self.assertIn("statement", payload["problem"])

    def test_csv_has_bom_header_and_both_kinds(self) -> None:
        result = api.export_problem(self.db, self.pid, "csv")
        self.assertTrue(result["content"].startswith("\ufeff"), "缺 BOM，Excel 打开中文会乱码")
        rows = list(csv.DictReader(io.StringIO(result["content"].lstrip("\ufeff"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["kind"], "dataset")
        self.assertEqual(rows[-1]["kind"], "adversarial")
        self.assertEqual(rows[-1]["actual_output"], "")
        self.assertIn("input", rows[0])

    def test_export_without_data_is_rejected(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE problems SET dataset_version=0 WHERE id=?", (self.pid,))
        result = api.export_problem(self.db, self.pid, "json")
        self.assertEqual(result["code"], "NO_DATA")

    def test_export_unknown_problem(self) -> None:
        self.assertEqual(api.export_problem(self.db, 9999, "json")["code"], "NOT_FOUND")


class TestDelete(Base):
    def test_delete_with_data_requires_title(self) -> None:
        result = api.delete_problem(self.db, self.pid)
        self.assertEqual(result["code"], "CONFIRM_REQUIRED")
        self.assertEqual(result["title"], "两数之和")
        self.assertEqual(result["cases"], 2)
        self.assertEqual(result["adversarial"], 1)
        # 没确认之前一条都不能删
        self.assertEqual(self._count("problems", "id=?", (self.pid,)), 1)

        wrong = api.delete_problem(self.db, self.pid, "两数")
        self.assertEqual(wrong["code"], "CONFIRM_REQUIRED")

    def test_delete_with_title_removes_children(self) -> None:
        self.assertTrue(api.delete_problem(self.db, self.pid, "两数之和")["ok"])
        for table, where, params in [
            ("problems", "id=?", (self.pid,)),
            ("test_cases", "problem_id=?", (self.pid,)),
            ("adversarial_cases", "problem_id=?", (self.pid,)),
            ("submissions", "problem_id=?", (self.pid,)),
            ("dataset_plans", "problem_id=?", (self.pid,)),
            ("jobs", "problem_id=?", (self.pid,)),
            ("case_reviews", "1=1", ()),
            ("adversarial_reviews", "1=1", ()),
        ]:
            self.assertEqual(self._count(table, where, params), 0, f"{table} 有残留")

    def test_delete_draft_needs_no_title(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE problems SET dataset_version=0 WHERE id=?", (self.pid,))
            conn.execute("UPDATE test_cases SET status='rejected' WHERE problem_id=?", (self.pid,))
            conn.execute("UPDATE adversarial_cases SET status='rejected' WHERE problem_id=?", (self.pid,))
        self.assertTrue(api.delete_problem(self.db, self.pid)["ok"])


if __name__ == "__main__":
    unittest.main()
