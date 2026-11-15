"""合并“我的数据”测试（契约 §4.2）：入库、去重、参考解不一致拒绝、force 放行。

用真实临时库 + 真实执行（参考解写成 `print(n*2)`），不 mock 判题。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autocode.store.db import Database, reset_default_db


class MergeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "autocode.db"
        self.env = patch.dict(os.environ, {"AUTOCODE_DB": str(self.db_path)}, clear=False)
        self.env.start()
        reset_default_db()
        self.db = Database(self.db_path)
        self.db.init_schema()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, spec_json, status, created_at, updated_at)"
                " VALUES('倍数', '输入一个整数，输出它的两倍。', ?, 'prepared', 'now', 'now')",
                (json.dumps({"checker_type": "exact", "samples": []}),),
            )
            self.pid = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO reference_solutions(problem_id, lang, role, code, verified,"
                " samples_expected, verification_level, created_at)"
                " VALUES(?, 'python', 'reference', ?, 1, 1, 'cross_verified', 'now')",
                (self.pid, "import sys\nprint(int(sys.stdin.read().strip()) * 2)\n"),
            )

    def tearDown(self) -> None:
        reset_default_db()
        self.env.stop()
        self.db.close()
        self._tmp.cleanup()

    def merge(self, cases, **kwargs):
        from autocode.web import api

        return api.merge_cases({"problem_id": self.pid, "cases": cases, **kwargs})

    def case_rows(self):
        return self.db.query_all(
            "SELECT layer, input_data, expected_output, source, trust_level FROM test_cases"
            " WHERE problem_id=? ORDER BY ordinal",
            (self.pid,),
        )


class TestAccepted(MergeCase):
    def test_correct_case_inserted(self) -> None:
        result = self.merge([{"input": "3", "output": "6"}])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["rejected"], [])
        rows = self.case_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["layer"], "user")
        self.assertEqual(rows[0]["source"], "user")
        self.assertEqual(rows[0]["trust_level"], "cross_verified")
        self.assertEqual(rows[0]["expected_output"].strip(), "6")

    def test_batch_of_cases(self) -> None:
        result = self.merge(
            [
                {"input": "1", "output": "2"},
                {"input": "2", "output": "4"},
                {"input": "10", "output": "20"},
            ]
        )
        self.assertEqual(result["added"], 3)
        self.assertEqual(len(self.case_rows()), 3)


class TestDedup(MergeCase):
    def test_duplicate_in_payload_skipped(self) -> None:
        result = self.merge([{"input": "3", "output": "6"}, {"input": "3", "output": "6"}])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_duplicate_against_db_skipped(self) -> None:
        self.merge([{"input": "3", "output": "6"}])
        result = self.merge([{"input": "3", "output": "6"}])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)


class TestRejected(MergeCase):
    def test_wrong_output_rejected_with_detail(self) -> None:
        result = self.merge([{"input": "3", "output": "7"}])
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["rejected"]), 1)
        item = result["rejected"][0]
        self.assertEqual(item["index"], 0)
        self.assertEqual(item["reason"], "参考解不一致")
        self.assertEqual(str(item["expected"]).strip(), "7")
        self.assertEqual(str(item["actual"]).strip(), "6")
        self.assertEqual(self.case_rows(), [])

    def test_mixed_batch_only_good_one_inserted(self) -> None:
        result = self.merge(
            [{"input": "3", "output": "6"}, {"input": "4", "output": "9"}]
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(self.case_rows()[0]["input_data"], "3")

    def test_empty_input_rejected(self) -> None:
        result = self.merge([{"input": "   ", "output": "6"}])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "input 为空")

    def test_empty_output_rejected_even_with_force(self) -> None:
        """S1：空输出入库等于制造无法判题的数据，force 也不放行。"""
        result = self.merge([{"input": "3", "output": "   "}], force=True)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["rejected"], [{"index": 0, "reason": "output 为空"}])
        self.assertEqual(self.case_rows(), [])

    def test_layer_whitelist_rejects_other_layers(self) -> None:
        """S5：不允许借合并接口往 boundary/stress 写数据。"""
        result = self.merge([{"input": "3", "output": "6"}], layer="boundary")
        self.assertIn("layer 只允许", result["error"])
        self.assertEqual(self.case_rows(), [])

    def test_over_length_rejected(self) -> None:
        result = self.merge([{"input": "1" * 20001, "output": "6"}])
        self.assertEqual(result["added"], 0)
        self.assertIn("超过长度上限", result["rejected"][0]["reason"])

    def test_no_cases_returns_error(self) -> None:
        self.assertEqual(self.merge([]).get("error"), "cases 不能为空")

    def test_unknown_problem(self) -> None:
        from autocode.web import api

        self.assertEqual(api.merge_cases({"problem_id": 999, "cases": [{"input": "1"}]}).get("error"), "题目不存在")


class TestConcurrentMerge(MergeCase):
    def test_ordinals_unique_under_concurrency(self) -> None:
        """S4：并发 merge 不能产生重复 ordinal（MAX 在写事务内取）。"""
        import threading

        from autocode.web import api

        errors: list[str] = []
        lock = threading.Lock()

        def worker(offset: int) -> None:
            try:
                api.merge_cases(
                    {
                        "problem_id": self.pid,
                        "cases": [
                            {
                                "input": str(value),
                                "output": str(2 * value),
                            }
                            for value in (offset * 10 + i for i in range(3))
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        import gc

        gc.collect()  # 线程里的 per-thread 连接需尽快释放，否则 Windows 删不掉临时库

        self.assertEqual(errors, [])
        rows = self.case_rows()
        self.assertGreater(len(rows), 0)
        ordinals = [
            int(row["ordinal"])
            for row in self.db.query_all(
                "SELECT ordinal FROM test_cases WHERE problem_id=? AND layer='user'", (self.pid,)
            )
        ]
        self.assertEqual(len(ordinals), len(set(ordinals)), ordinals)


class TestForce(MergeCase):
    def test_force_ignores_verification(self) -> None:
        result = self.merge([{"input": "3", "output": "7"}], force=True)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["rejected"], [])
        row = self.case_rows()[0]
        self.assertEqual(row["expected_output"].strip(), "7")  # 用户值原样保留
        self.assertEqual(row["trust_level"], "reference_only")

    def test_no_verified_solution_rejected_unless_force(self) -> None:
        self.db.execute(
            "UPDATE reference_solutions SET verified=0 WHERE problem_id=?", (self.pid,)
        )
        result = self.merge([{"input": "3", "output": "6"}])
        self.assertEqual(result["added"], 0)
        self.assertIn("没有已验证参考解", result["rejected"][0]["reason"])
        forced = self.merge([{"input": "5", "output": "10"}], force=True)
        self.assertEqual(forced["added"], 1)


if __name__ == "__main__":
    unittest.main()
