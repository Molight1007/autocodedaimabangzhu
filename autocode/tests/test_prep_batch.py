"""批量预生成测试：失败归类、单题不中断、全局预算跳过、汇总表渲染。

桩客户端只返回"坏 spec"，所以不会真的执行代码，跑得很快。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.app.prep_batch import (
    CLASS_BUDGET,
    CLASS_CROSS,
    CLASS_GENERATOR,
    CLASS_GRAMMAR,
    CLASS_OTHER,
    CLASS_REF_SAMPLE,
    CLASS_SPEC,
    CLASS_TIMEOUT,
    STATUS_FAILED,
    STATUS_SKIPPED,
    classify_failure,
    run_batch,
)
from autocode.app.prep_models import ARTIFACT_VERSION
from autocode.llm.client import ChatResult, ChatUsage
from autocode.store.db import Database

BAD_SPEC = [{"kind": "graph", "name": "g"}]  # grammar 不支持 → spec 阶段必失败


class BadSpecClient:
    def __init__(self) -> None:
        self.usage = ChatUsage()

    def chat(self, messages: List[Dict[str, str]], *, json_mode: bool = True, tag: str = "", **_kw: Any) -> ChatResult:
        usage = ChatUsage(100, 20, 120)
        self.usage.add(usage)
        payload = {"input_grammar": BAD_SPEC}
        return ChatResult(content=json.dumps(payload), usage=usage, attempts=1, elapsed_ms=1.0,
                          model="stub", json_obj=payload, finish_reason="stop")


class BatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()
        for index in range(3):
            self.db.execute(
                "INSERT INTO problems(title, source, source_id, statement_md, status, spec_json,"
                " created_at, updated_at) VALUES(?, 'test', ?, ?, 'draft', ?, 'now', 'now')",
                (
                    f"题{index + 1}",
                    f"T-{index + 1}",
                    f"## 题{index + 1}（t{index + 1}）\n\n### 【样例输入】\n3\n1 2 3\n### 【样例输出】\n6\n",
                    json.dumps({"samples": [{"input": "3\n1 2 3\n", "output": "6\n"}]}),
                ),
            )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


class TestClassify(unittest.TestCase):
    def test_grammar(self) -> None:
        self.assertEqual(
            classify_failure("spec", "input_grammar: 节点[3] count 引用未知 scalar 'n-1'"),
            CLASS_GRAMMAR,
        )

    def test_spec_other(self) -> None:
        self.assertEqual(classify_failure("spec", "LLM 未返回 JSON 对象"), CLASS_SPEC)

    def test_reference(self) -> None:
        self.assertEqual(classify_failure("references", "reference(python) 连续 3 次未通过样例"), CLASS_REF_SAMPLE)

    def test_cross(self) -> None:
        self.assertEqual(classify_failure("references", "小规模对拍不一致（分歧 2 条）"), CLASS_CROSS)

    def test_generator(self) -> None:
        self.assertEqual(classify_failure("generator", "generator 连续 3 次未通过"), CLASS_GENERATOR)

    def test_timeout(self) -> None:
        self.assertEqual(classify_failure("timeout", "单题超过时限"), CLASS_TIMEOUT)

    def test_budget(self) -> None:
        self.assertEqual(classify_failure("budget", "LLM 调用已达上限"), CLASS_BUDGET)

    def test_other(self) -> None:
        self.assertEqual(classify_failure("datasets", "什么都行"), CLASS_OTHER)


class TestSelectTargets(BatchCase):
    def test_includes_stale_preparing(self) -> None:
        """被中断的批量会留下 preparing，必须能被再次选中（否则静默丢题）。"""
        from autocode.app.prep_batch import select_targets

        self.db.execute("UPDATE problems SET status='preparing' WHERE id=2")
        self.db.execute(
            "UPDATE problems SET status='prepared', artifact_version=? WHERE id=3",
            (ARTIFACT_VERSION,),
        )
        ids = [row["id"] for row in select_targets(self.db)]
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertNotIn(3, ids)

    def test_include_prepared_flag(self) -> None:
        from autocode.app.prep_batch import select_targets

        self.db.execute(
            "UPDATE problems SET status='prepared', artifact_version=?", (ARTIFACT_VERSION,)
        )
        self.assertEqual(select_targets(self.db), [])
        self.assertEqual(len(select_targets(self.db, include_prepared=True)), 3)

    def test_stale_prepared_is_selected_without_force(self) -> None:
        """prepared 但版本低 = 旧版产物，必须自动被选中升级（不再依赖 --force）。"""
        from autocode.app.prep_batch import select_targets

        self.db.execute(
            "UPDATE problems SET status='prepared', artifact_version=?", (ARTIFACT_VERSION,)
        )
        self.db.execute(
            "UPDATE problems SET artifact_version=? WHERE id=1", (ARTIFACT_VERSION - 1,)
        )
        self.assertEqual([row["id"] for row in select_targets(self.db)], [1])


class TestRunBatch(BatchCase):
    def test_failures_do_not_abort_batch(self) -> None:
        report = run_batch(
            self.db, client=BadSpecClient(), budget_per=3, budget_total=100,
            timeout_sec=60, allow_parser_fallback=False, enable_llm_extras=False,
        )
        self.assertEqual(len(report.rows), 3)
        self.assertEqual(report.failed_count, 3)
        self.assertEqual(report.ok_count, 0)
        self.assertTrue(all(row.status == STATUS_FAILED for row in report.rows))
        self.assertTrue(all(row.failed_stage == "spec" for row in report.rows))
        self.assertTrue(all(row.category == CLASS_GRAMMAR for row in report.rows))
        self.assertTrue(all(row.calls > 0 for row in report.rows))

    def test_global_budget_skips_rest(self) -> None:
        report = run_batch(
            self.db, client=BadSpecClient(), budget_per=5, budget_total=2, timeout_sec=60
        )
        statuses = [row.status for row in report.rows]
        self.assertEqual(statuses[0], STATUS_FAILED)
        self.assertIn(STATUS_SKIPPED, statuses)
        self.assertTrue(report.budget_exhausted)
        self.assertGreaterEqual(report.skipped_count, 1)
        skipped = [row for row in report.rows if row.status == STATUS_SKIPPED]
        self.assertTrue(all(row.category == CLASS_BUDGET for row in skipped))
        self.assertEqual(skipped[0].calls, 0)

    def test_table_renders_all_rows(self) -> None:
        report = run_batch(
            self.db, client=BadSpecClient(), budget_per=2, budget_total=100, timeout_sec=60
        )
        table = report.render_table()
        self.assertIn("失败阶段", table)
        for row in report.rows:
            self.assertIn(row.title, table)

    def test_report_serializable(self) -> None:
        report = run_batch(
            self.db, client=BadSpecClient(), budget_per=2, budget_total=100, timeout_sec=60
        )
        payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertIn("categories", payload)
        self.assertEqual(payload["failed_count"], 3)

    def test_db_records_failed_stage(self) -> None:
        # 单题上限给足（5 > spec 的 3 次尝试），这样失败阶段应停在 spec 而不是 budget
        run_batch(self.db, client=BadSpecClient(), budget_per=5, budget_total=100, timeout_sec=60)
        rows = self.db.query_all("SELECT status, failed_stage FROM problems ORDER BY id")
        for row in rows:
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["failed_stage"], "spec")

    def test_per_problem_cap_stops_at_budget(self) -> None:
        run_batch(self.db, client=BadSpecClient(), budget_per=2, budget_total=100, timeout_sec=60)
        rows = self.db.query_all("SELECT status, failed_stage FROM problems ORDER BY id")
        for row in rows:
            self.assertEqual(row["failed_stage"], "budget")


if __name__ == "__main__":
    unittest.main()
