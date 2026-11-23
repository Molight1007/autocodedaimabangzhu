"""D0 题面补全的契约测试。

测的是"补全必须只补格式"这条底线，不是模型好坏：
- 保留原文主体、只加输入输出段的 → 通过；
- 把题意改写一遍的 → 必须拒；
- 模型自己说补不了的 → 透传原因，不硬编。

另外覆盖落库行为：补全后原文留档、标记置 1，能还原。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autocode.slim2 import config, repair
from autocode.slim2.dataset import run_dataset
from autocode.slim2.db import Database

ORIGINAL = (
    "石头剪刀布是常见的猜拳游戏。石头胜剪刀，剪刀胜布，布胜石头。"
    "如果两个人出拳一样，则不分胜负。\n\n"
    "一天，小A和小B正好在玩石头剪刀布。已知他们的出拳都是有周期性规律的。"
    "请问，小A和小B比了N轮之后，谁赢的轮数多？"
)

FIXED = ORIGINAL + """

【输入格式】
第一行一个整数 N。第二行一个只含 R/S/P 的字符串，表示小A的周期；第三行同格式，表示小B的周期。

【输出格式】
输出一个字符：A、B 或 D（平局）。

【样例输入】
5
RSP
SPR

【样例输出】
D

【数据范围】
1 ≤ N ≤ 10^5，1 ≤ 周期长度 ≤ 100"""


class TestCheck(unittest.TestCase):
    def _payload(self, **over):
        base = {
            "complete": True,
            "statement": FIXED,
            "added": ["补充了输入格式：第一行 N，随后两行周期串", "补充了数据范围"],
            "assumptions": ["原题未说明周期如何给出，按字符串处理"],
            "confidence": "high",
            "unsupported_reason": "",
        }
        base.update(over)
        return base

    def test_accepts_complete_repair(self) -> None:
        result = repair.check(self._payload(), ORIGINAL)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.confidence, "high")
        self.assertTrue(result.assumptions)

    def test_rejects_rewritten_statement(self) -> None:
        """新题面没保留原文片段 → 判定改写了题意。"""
        result = repair.check(
            self._payload(statement="给定一个长度为 n 的数组，求最大子段和。" * 20),
            ORIGINAL,
        )
        self.assertFalse(result.ok)
        self.assertIn("题意", result.reason)

    def test_complete_false_passes_reason_through(self) -> None:
        result = repair.check(
            {"complete": False, "unsupported_reason": "题面只有标题，不知道要算什么"},
            ORIGINAL,
        )
        self.assertFalse(result.ok)
        self.assertIn("标题", result.reason)

    def test_requires_added_list(self) -> None:
        result = repair.check(self._payload(added=[]), ORIGINAL)
        self.assertFalse(result.ok)
        self.assertIn("added", result.reason)

    def test_short_statement_must_grow(self) -> None:
        result = repair.check(self._payload(statement=ORIGINAL), ORIGINAL)
        self.assertFalse(result.ok)
        self.assertIn("几乎没补", result.reason)


SHORT = "给定两个整数，请你计算它们的和。"

#: 与 GOOD_CODE 自洽：一行两个整数，输出它们的和
FIXED_SHORT = SHORT + """

【输入格式】
一行两个整数 a 和 b，用空格分隔。

【输出格式】
一个整数，表示 a + b。

【样例输入】
1 2

【样例输出】
3

【数据范围】
-10^9 ≤ a, b ≤ 10^9"""

GOOD_CODE = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n"


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
        "analysis": {"input_model": "一行两个整数", "contract_note": "", "unsupported_reason": ""},
        "plan": {"target_count": 10, "layers": {"boundary": 3, "typical": 4, "stress": 2, "trap": 1},
                 "coverage": ["0", "负数", "上界"]},
        "cases": [{"layer": l, "size_class": s, "input": d, "why": "测试"} for l, s, d in rows],
    }


class TestRepairInPipeline(unittest.TestCase):
    """题面残缺时，run_dataset 要先补全再生成，并把这件事写进结果。"""

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
                ("石头剪刀布", SHORT, "hash-short"),
            )
            self.problem_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _runner(self):
        seen = {"repair": 0}

        def respond(stage: str, user: str, _system: str):
            if stage == "slim_repair":
                seen["repair"] += 1
                return {
                    "complete": True,
                    "statement": FIXED_SHORT,
                    "added": ["补充了输入格式", "补充了数据范围"],
                    "assumptions": ["周期按字符串给出"],
                    "confidence": "medium",
                }
            if stage == "slim_reference":
                return {"code": GOOD_CODE}
            if stage == "slim_dataset":
                return _payload()
            if stage == "slim_dataset_review":
                import re

                indices = [int(item) for item in re.findall(r"\[index=(\d+)\]", user)]
                return {
                    "contract": "一行两个整数",
                    "reviews": [
                        {"index": i, "input_ok": True, "recompute": "心算",
                         "recomputed_output": None, "verdict": "uncertain",
                         "reason": "数字较大", "corrected_output": None}
                        for i in indices
                    ],
                    "coverage": {"missing": [], "verdict": "ok"},
                }
            raise AssertionError(f"未知阶段 {stage}")

        from autocode.slim2.llm_stage import StageRunner

        return StageRunner(self.db, problem_id=self.problem_id, budget=10, responder=respond), seen

    def test_short_statement_is_repaired_before_generation(self) -> None:
        runner, seen = self._runner()
        result = run_dataset(self.db, self.problem_id, runner)
        self.assertEqual(seen["repair"], 1, "短题面必须先调一次补全")
        self.assertIn("题面已由模型补全", result["warnings"][0])
        row = self.db.query_one(
            "SELECT statement_repaired, statement_original, statement_md FROM problems WHERE id=?",
            (self.problem_id,),
        )
        self.assertEqual(int(row["statement_repaired"]), 1)
        self.assertEqual(str(row["statement_original"]), SHORT)
        self.assertIn("【输入格式】", str(row["statement_md"]))


if __name__ == "__main__":
    unittest.main()
