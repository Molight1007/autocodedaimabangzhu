"""没有题面样例的题也要能生成数据（默认放开，可用环境变量改回严格模式）。

背景（2026-11-22）：自编题（老师手里没给样例）在导入后点「生成测试数据」直接被拒，
提示"没有解析出样例，无法校准期望输出"。而系统本来对"标程跑不起来"是容忍的
（engaged=False 只是降级提示），单卡样例这一条不一致。

放开之后的语义必须钉死：
- 生成照常跑，代码答案照常执行（`engaged=True`），但 `anchored=False`；
- 不会凭空多出 source='sample' 的用例；
- 需要严格模式时设 `AUTOCODE_SLIM2_REQUIRE_SAMPLES=1`，行为退回旧版（直接失败）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from autocode.slim2 import config
from autocode.slim2.dataset import run_dataset
from autocode.slim2.errors import StageFailure
from autocode.slim2.llm_stage import StageRunner
from autocode.store.db import Database

TITLE = "两数之和"
STATEMENT = """两数之和
【题目描述】
给定两个整数 a 和 b，输出它们的和。
【输入格式】
一行两个整数 a b。
【输出格式】
一个整数，表示 a+b。
"""


def _cases() -> list:
    """12 条合法分层数据（层下限：boundary 3 / typical 3 / stress 1 / trap 1）。"""
    rows = [
        ("boundary", "tiny", "0 0\n", "0\n"),
        ("boundary", "tiny", "-1 0\n", "-1\n"),
        ("boundary", "tiny", "1000000000 1000000000\n", "2000000000\n"),
        ("typical", "small", "1 2\n", "3\n"),
        ("typical", "small", "-3 5\n", "2\n"),
        ("typical", "small", "10 -10\n", "0\n"),
        ("typical", "small", "7 8\n", "15\n"),
        ("stress", "large", "999999999 1\n", "1000000000\n"),
        ("stress", "large", "-999999999 -1\n", "-1000000000\n"),
        ("trap", "tiny", "0 -0\n", "0\n"),
        ("trap", "tiny", "-1000000000 -1000000000\n", "-2000000000\n"),
        ("trap", "tiny", "1 -1\n", "0\n"),
    ]
    return [
        {"layer": layer, "size_class": size, "input": data,
         "derive": f"{data.strip()} 相加得 {expected.strip()}",
         "expected_output": expected, "confidence": "high"}
        for layer, size, data, expected in rows
    ]


REFERENCE = "import sys\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n"


def _responder():
    payload = {
        "io": {
            "input": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}],
            "output": {"type": "int", "order": "fixed"},
        },
        "judge_type": "token",
        "samples": [],
        "analysis": {
            "input_model": "一行两个整数",
            "answer_model": "一个整数：和",
            "constraint_dims": [{"name": "a,b", "min": "-1e9", "max": "1e9"}],
            "risk_points": ["负负得负", "0"],
            "unsupported_reason": "",
        },
        "plan": {
            "target_count": 12,
            "layers": {"boundary": 3, "typical": 4, "stress": 2, "trap": 3},
            "coverage": ["0", "负数", "上界"],
        },
        "cases": _cases(),
    }

    def respond(stage: str, user: str, _system: str):
        if stage == "slim_dataset":
            # 题面带样例时，契约下必须给出"重写后的样例"（组数要一一对应）；
            # 没有样例的题则给空数组——两种都是有意的，不能互相顶替。
            payload["samples"] = (
                [{"input": "1 2\n", "output": "3\n"}] if "[样例1]" in user else []
            )
            return payload
        if stage == "slim_dataset_review":
            indices = [int(item) for item in re.findall(r"\[index=(\d+)\]", user)]
            return {
                "contract": "一行两个整数，输出和",
                "reviews": [
                    {"index": index, "input_ok": True, "recompute": "两数相加",
                     "recomputed_output": _cases()[index]["expected_output"],
                     "verdict": "pass", "reason": "", "corrected_output": None}
                    for index in indices
                ],
                "coverage": {"missing": [], "verdict": "ok"},
            }
        if stage == "slim_reference":
            return {"code": REFERENCE}
        raise AssertionError(f"未知阶段 {stage}")

    return respond


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 自建库，不走 slim2.db.get_db：后者 apply_env() 会改写全局 AUTOCODE_DB
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        self._old_env = os.environ.pop("AUTOCODE_SLIM2_REQUIRE_SAMPLES", None)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, statement_hash, samples_json,"
                " constraints_json, judge_type, tolerance, multi_testcase, layout_json,"
                " status, dataset_version, created_at, updated_at)"
                " VALUES(?,?,?, '[]', '[]', 'token', '', 0, '[]', 'draft', 0,"
                " '2026-11-22T00:00:00+00:00', '2026-11-22T00:00:00+00:00')",
                (TITLE, STATEMENT, "hash-" + TITLE),
            )
            self.problem_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()
        os.environ.pop("AUTOCODE_SLIM2_REQUIRE_SAMPLES", None)
        if self._old_env is not None:
            os.environ["AUTOCODE_SLIM2_REQUIRE_SAMPLES"] = self._old_env

    def _run(self) -> dict:
        runner = StageRunner(
            self.db, problem_id=self.problem_id, budget=8, responder=_responder()
        )
        return run_dataset(self.db, self.problem_id, runner)


class TestGenerateWithoutSamples(_Base):
    def test_generates_and_flags_unanchored(self) -> None:
        result = self._run()
        self.assertGreaterEqual(int(result["accepted"]), config.MIN_CASES)
        quality = json.loads(
            str(self.db.query_one("SELECT quality_json FROM problems WHERE id=?",
                                  (self.problem_id,))[0])
        )
        run_check = quality["run_verification"]
        self.assertFalse(run_check["anchored"], "无样例必须标成未锚定")
        # 2026-11-22：没有样例只表示"缺锚"，代码答案仍要逐条执行
        self.assertTrue(run_check["engaged"], "代码答案应当照常执行")
        self.assertGreaterEqual(int(run_check["executed"]), config.MIN_CASES)
        self.assertIn("样例", run_check["reason"])

    def test_no_sample_cases_invented(self) -> None:
        self._run()
        sample_rows = self.db.query_all(
            "SELECT id FROM test_cases WHERE problem_id=? AND source='sample'",
            (self.problem_id,),
        )
        self.assertEqual(sample_rows, [], "没有样例就不该凭空写出 source='sample' 的用例")

    def test_strict_mode_still_refuses(self) -> None:
        os.environ["AUTOCODE_SLIM2_REQUIRE_SAMPLES"] = "1"
        with self.assertRaises(StageFailure) as ctx:
            self._run()
        self.assertEqual(ctx.exception.stage, "input")
        self.assertIn("没有解析出样例", ctx.exception.message)


class TestHealStaleSamples(_Base):
    """老题的 samples_json 是导入那一刻算的：规则升级后要能自动补算。

    现场症状（2026-11-22 截图）：题面明明写着 `示例 1：输入：... 输出：...`，
    题目页仍报"没有解析出样例"。原因是那一行是旧解析器写进库的 0 组；
    重新导入又会被 statement_hash 判成重复题，只返回旧 problem_id，样例永远不更新。
    """

    def test_stale_row_is_healed_on_generate(self) -> None:
        statement = (
            "两数之和\n示例 1：\n输入：1 2\n输出：3\n"
            "提示：\n1 <= a, b <= 10\n"
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE problems SET statement_md=? WHERE id=?",
                (statement, self.problem_id),
            )
        result = self._run()
        self.assertGreaterEqual(int(result["accepted"]), config.MIN_CASES)

        healed = json.loads(
            str(self.db.query_one("SELECT samples_json FROM problems WHERE id=?",
                                  (self.problem_id,))[0])
        )
        self.assertEqual(len(healed), 1, f"老题样例没补上：{healed}")
        self.assertEqual(healed[0]["output"].strip(), "3")
        quality = json.loads(
            str(self.db.query_one("SELECT quality_json FROM problems WHERE id=?",
                                  (self.problem_id,))[0])
        )
        self.assertTrue(quality["run_verification"]["anchored"], "补到样例后应当能锚定")


class TestFailureText(unittest.TestCase):
    def test_strict_flag_switch(self) -> None:
        os.environ.pop("AUTOCODE_SLIM2_REQUIRE_SAMPLES", None)
        self.assertFalse(config.require_samples())
        os.environ["AUTOCODE_SLIM2_REQUIRE_SAMPLES"] = "1"
        self.assertTrue(config.require_samples())
        os.environ["AUTOCODE_SLIM2_REQUIRE_SAMPLES"] = "0"
        self.assertFalse(config.require_samples())
        os.environ.pop("AUTOCODE_SLIM2_REQUIRE_SAMPLES", None)


if __name__ == "__main__":
    unittest.main()
