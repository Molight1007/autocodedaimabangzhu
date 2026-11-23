"""新版数据流水的接线测试：代码答案先写、失败重试一次、答案由执行产生。

不调真实模型（用 responder 桩），但代码是**真跑**的 —— 这组测试要证明的是：
- 没有样例时不再阻断，代码照常执行并产出答案；
- 有样例但代码没过样例时，会带着失败原因重写一次；
- 两次都不可用就整题失败，不产出可疑数据。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autocode.slim2 import config
from autocode.slim2.dataset import run_dataset
from autocode.slim2.errors import StageFailure
from autocode.slim2.llm_stage import StageRunner
from autocode.store.db import Database

STATEMENT = """两数之和
【题目描述】
给定两个整数 a 和 b，输出它们的和。
【输入格式】
一行两个整数 a b。
【输出格式】
一个整数，表示 a+b。
【样例输入】
1 2
【样例输出】
3
"""

NO_SAMPLE_STATEMENT = """两数之和
【题目描述】
给定两个整数 a 和 b，输出它们的和。
【输入格式】
一行两个整数 a b。
【输出格式】
一个整数，表示 a+b。
"""

GOOD_CODE = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n"
WRONG_CODE = "print(0)\n"


def _payload() -> dict:
    rows = [
        ("boundary", "tiny", "0 0\n"),
        ("boundary", "tiny", "-1 0\n"),
        ("boundary", "tiny", "1000000000 1000000000\n"),
        ("typical", "tiny", "1 3\n"),
        ("typical", "tiny", "-3 5\n"),
        ("typical", "tiny", "10 -10\n"),
        ("typical", "tiny", "7 8\n"),
        ("stress", "tiny", "999999999 1\n"),
        ("stress", "tiny", "-999999999 -1\n"),
        ("trap", "tiny", "1 -1\n"),
    ]
    return {
        "io": {
            "input": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}],
            "output": {"type": "int", "order": "fixed"},
        },
        "judge_type": "token",
        "samples": [{"input": "1 2\n", "output": "3\n"}],
        "analysis": {
            "input_model": "一行两个整数",
            "contract_note": "",
            "unsupported_reason": "",
        },
        "plan": {
            "target_count": 10,
            "layers": {"boundary": 3, "typical": 4, "stress": 2, "trap": 1},
            "coverage": ["0", "负数", "上界"],
        },
        "cases": [
            {"layer": layer, "size_class": size, "input": data, "why": "测试两数相加"}
            for layer, size, data in rows
        ],
    }


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, statement_hash, samples_json,"
                " constraints_json, judge_type, tolerance, multi_testcase, layout_json,"
                " status, dataset_version, created_at, updated_at)"
                " VALUES(?,?,?, '[]', '[]', 'token', '', 0, '[]', 'draft', 0,"
                " '2026-11-22T00:00:00+00:00', '2026-11-22T00:00:00+00:00')",
                ("两数之和", STATEMENT, "hash-flow"),
            )
            self.problem_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _runner(self, codes, review_verdict="uncertain", with_samples=True,
                d2_samples=False):
        seen = {"reference": 0, "users": []}

        def respond(stage: str, user: str, _system: str):
            if stage == "slim_reference":
                seen["users"].append(user)
                index = min(seen["reference"], len(codes) - 1)
                seen["reference"] += 1
                return {"code": codes[index]}
            if stage == "slim_dataset":
                payload = _payload()
                # d2_samples=True 模拟真实情形：题面解析不出样例，D2 按契约重写一组
                if not with_samples and not d2_samples:
                    payload["samples"] = []
                return payload
            if stage == "slim_dataset_review":
                indices = [int(item) for item in re.findall(r"\[index=(\d+)\]", user)]
                return {
                    "contract": "一行两个整数，输出和",
                    "reviews": [
                        {"index": index, "input_ok": True,
                         "recompute": "两个数相加，心算核对",
                         "recomputed_output": None if review_verdict == "uncertain" else "0\n",
                         "verdict": review_verdict, "reason": "数字较大" if review_verdict == "uncertain" else "结果不同",
                         "corrected_output": None if review_verdict == "uncertain" else "0\n"}
                        for index in indices
                    ],
                    "coverage": {"missing": [], "verdict": "ok"},
                }
            raise AssertionError(f"未知阶段 {stage}")

        runner = StageRunner(self.db, problem_id=self.problem_id, budget=10, responder=respond)
        return runner, seen

    def _quality(self) -> dict:
        row = self.db.query_one("SELECT quality_json FROM problems WHERE id=?", (self.problem_id,))
        return json.loads(str(row[0]))


class TestCodeFirstFlow(_Base):
    def test_retry_code_once_when_sample_fails(self) -> None:
        runner, seen = self._runner([WRONG_CODE, GOOD_CODE])
        result = run_dataset(self.db, self.problem_id, runner)
        self.assertGreaterEqual(int(result["accepted"]), config.MIN_CASES)
        self.assertEqual(seen["reference"], 2, "第一次代码没过样例，应当重写一次")
        self.assertIn("上次失败", seen["users"][1], "重写时要把失败原因带上")
        quality = self._quality()
        self.assertTrue(quality["run_verification"]["engaged"])
        self.assertTrue(quality["run_verification"]["anchored"])
        self.assertEqual(quality["run_verification"]["samples"], "1/1")
        # 答案必须来自代码执行：库里的期望输出逐条等于 a+b
        rows = self.db.query_all(
            "SELECT input_data, expected_output FROM test_cases"
            " WHERE problem_id=? AND source='generated' AND status='active'",
            (self.problem_id,),
        )
        self.assertGreaterEqual(len(rows), config.MIN_CASES)
        for row in rows:
            a, b = (int(item) for item in str(row["input_data"]).split())
            self.assertEqual(int(str(row["expected_output"]).strip()), a + b)

    def test_two_failures_abort_without_writing_data(self) -> None:
        runner, seen = self._runner([WRONG_CODE, WRONG_CODE])
        with self.assertRaises(StageFailure) as ctx:
            run_dataset(self.db, self.problem_id, runner)
        self.assertEqual(ctx.exception.stage, "reference")
        self.assertIn("两次", ctx.exception.message)
        self.assertEqual(seen["reference"], 2)
        row = self.db.query_one(
            "SELECT dataset_version, status FROM problems WHERE id=?", (self.problem_id,)
        )
        self.assertEqual((int(row[0]), str(row[1])), (0, "draft"))
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM test_cases WHERE problem_id=?",
                                 (self.problem_id,), 0),
            0,
        )

    def test_batch_input_cap_refuses_oversize_batch(self) -> None:
        """体积上限防的是"一次生成把执行/复核拖爆"，超了就让 D2 重来。"""
        runner, _seen = self._runner([GOOD_CODE])
        with patch.object(config, "MAX_BATCH_INPUT_CHARS", 10):
            with self.assertRaises(StageFailure) as ctx:
                run_dataset(self.db, self.problem_id, runner)
        self.assertEqual(ctx.exception.stage, "dataset")
        self.assertIn("单批上限", ctx.exception.message)

    def test_batch_total_cap_refuses_oversize_outputs(self) -> None:
        """输入不大但答案很长（例如数组输出）时，输入+输出总量也要有上限。"""
        runner, _seen = self._runner([GOOD_CODE])
        with patch.object(config, "MAX_BATCH_CHARS", 10):
            with self.assertRaises(StageFailure) as ctx:
                run_dataset(self.db, self.problem_id, runner)
        self.assertEqual(ctx.exception.stage, "dataset")
        self.assertIn("输入+输出", ctx.exception.message)

    def test_review_conflict_without_anchor_drops_cases(self) -> None:
        # 没有样例：代码可执行但未锚定；复核给出不同结果 → 该条丢弃
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE problems SET statement_md=?, samples_json='[]' WHERE id=?",
                (NO_SAMPLE_STATEMENT, self.problem_id),
            )
        runner, _seen = self._runner(
            [GOOD_CODE], review_verdict="mismatch", with_samples=False
        )
        with self.assertRaises(StageFailure) as ctx:
            run_dataset(self.db, self.problem_id, runner)
        self.assertEqual(ctx.exception.stage, "dataset")
        self.assertIn("下限", ctx.exception.message)
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM test_cases WHERE problem_id=?",
                                 (self.problem_id,), 0),
            0,
        )


    def test_no_statement_samples_records_contract_samples(self) -> None:
        """题面解析不出样例、D2 按契约重写一组时，要记下"代码跑通了这组"。

        `anchored` 仍然为 false（那是题面原文才有的可信度），但界面不能
        显示成"完全没验过"——实测题 28 就是这种情形。
        """
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE problems SET statement_md=?, samples_json='[]' WHERE id=?",
                (NO_SAMPLE_STATEMENT, self.problem_id),
            )
        runner, _seen = self._runner(
            [GOOD_CODE], review_verdict="uncertain", with_samples=False, d2_samples=True
        )
        result = run_dataset(self.db, self.problem_id, runner)
        check = result["run_verification"]
        self.assertFalse(check["anchored"])
        self.assertEqual(check["contract_samples"], "1/1")


class TestStatementGuard(unittest.TestCase):
    """题面残缺时要在入口拦下：实测这种题会烧 3 次调用、写满 32k token 再截断。"""

    def test_short_statement_without_io_is_rejected(self) -> None:
        from autocode.slim2 import dataset

        trouble = dataset._statement_trouble("石头剪刀布是常见的猜拳游戏，谁赢的轮数多？")
        self.assertTrue(trouble)
        self.assertIn("输入格式", trouble)

    def test_short_statement_with_io_passes(self) -> None:
        from autocode.slim2 import dataset

        self.assertEqual(dataset._statement_trouble("输入两个整数，输出它们的和。"), "")

    def test_long_statement_passes_even_without_keywords(self) -> None:
        from autocode.slim2 import dataset

        # 长题面可能把格式写在很靠后的位置，不能按字数一刀切
        self.assertEqual(dataset._statement_trouble("描述" * 200), "")


class TestLayerGapWarning(unittest.TestCase):
    def test_missing_stress_is_reported(self) -> None:
        from autocode.slim2 import dataset

        gaps = dataset._layer_gap_warnings(
            {"boundary": 4, "typical": 4, "stress": 0, "trap": 2}
        )
        self.assertTrue(gaps)
        self.assertIn("压力层 0/1", gaps[0])

    def test_complete_layers_say_nothing(self) -> None:
        from autocode.slim2 import dataset

        self.assertEqual(
            dataset._layer_gap_warnings(
                {"boundary": 3, "typical": 3, "stress": 1, "trap": 1}
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
