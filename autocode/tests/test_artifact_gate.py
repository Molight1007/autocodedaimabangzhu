"""产物门（v4 契约）的单元测试：不联网、不起进程，只造库内产物。

守的是「版本号必须是证据」这条线：
1. 半新产品（单解 / 旧版整段解析 / 提示不分方法 / 数据太少 / 缺题解）一律不通过；
2. 全部契约满足才通过，且判据只读库、不看内存里的"我生成了"；
3. `prepare` 在盖章前调用它——不通过就整题失败，不能落 artifact_version。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.app import artifact_gate
from autocode.app.prep_store import PrepStore
from autocode.store.db import Database


def _notes(rows: int = 6, coverage: str = "all") -> str:
    """造一份 v3 逐行解析；coverage='half' 时只讲一半非空行。"""
    lines = []
    for index in range(1, rows + 1):
        explained = coverage == "all" or index % 2 == 1
        lines.append({
            "n": index,
            "code": f"x{index} = {index}",
            "explain": f"第 {index} 行做点事" if explained else "",
        })
    return json.dumps({"v": 3, "summary": "s", "lines": lines, "pitfalls": []}, ensure_ascii=False)


class ArtifactGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()
        self.store = PrepStore(self.db)
        self.db.execute(
            "INSERT INTO problems(id, title, statement_md, analysis_json, status)"
            " VALUES(1, 't', 'stmt', ?, 'prepared')",
            (json.dumps({"plain_question": "q", "thinking_path": ["a"]}),),
        )
        # 基线：一份「完整 v4 产物」，各用例子测试只破坏其中一项
        self._ref(role="reference", approach="模拟", lang="python", notes=_notes())
        self._ref(role="alt1", approach="二分", lang="python", notes=_notes())
        self._ref(role="brute", approach="暴力枚举", lang="python", notes=None)
        self.db.executemany(
            "INSERT INTO hints(problem_id, level, approach, content, is_active, gen_batch_id)"
            " VALUES(1, ?, ?, 'x', 1, ?)",
            [(0, "", "b0"), (2, "模拟", "b1"), (3, "模拟", "b1"),
             (2, "二分", "b2"), (3, "二分", "b2")],
        )
        for layer, count in (("boundary", 4), ("exhaustive", 8)):
            self.db.executemany(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " expected_output, source_of_truth, trust_level)"
                " VALUES(1, ?, ?, ?, ?, 'e', 'oracle', 'oracle')",
                [(layer, i, f"i{i}", f"{layer}{i}") for i in range(count)],
            )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _ref(self, *, role: str, approach: str, lang: str, notes: str | None) -> None:
        self.db.execute(
            "INSERT INTO reference_solutions(problem_id, lang, role, code, verified,"
            " approach, line_notes_json) VALUES(1, ?, ?, 'code', 1, ?, ?)",
            (lang, role, approach, notes),
        )

    def issues(self) -> List[str]:
        return artifact_gate.audit(self.store, 1)

    # ── 通过 ────────────────────────────────────────────────────────────────
    def test_complete_artifacts_pass(self) -> None:
        self.assertEqual(self.issues(), [])

    # ── 单解 ────────────────────────────────────────────────────────────────
    def test_single_method_fails(self) -> None:
        self.db.execute("DELETE FROM reference_solutions WHERE role='alt1'")
        issues = self.issues()
        self.assertTrue(any("解法只有 1 种" in item for item in issues), issues)

    def test_brute_counts_when_it_is_a_different_implementation(self) -> None:
        """暴力解在界面里是独立一组（有自己的提示与逐行解析）⇒ 它是"第二种解法"。"""
        self.db.execute("DELETE FROM reference_solutions WHERE role='alt1'")
        self.db.execute(
            "UPDATE reference_solutions SET code=? WHERE role='brute'",
            ("import sys\nn = int(sys.stdin.readline())\nbest = 0\nfor i in range(n):\n"
             "    best = max(best, i)\nprint(best)\n",),
        )
        self.assertFalse(any("解法只有 1 种" in item for item in self.issues()))

    def test_reskinned_brute_does_not_count(self) -> None:
        """控制流和主解一样（只换变量名）的暴力解不算另一种思路。"""
        self.db.execute("DELETE FROM reference_solutions WHERE role='alt1'")
        self.db.execute(
            "UPDATE reference_solutions SET code=? WHERE role='reference'",
            ("import sys\nn = int(sys.stdin.readline())\nprint(n)\n",),
        )
        self.db.execute(
            "UPDATE reference_solutions SET code=? WHERE role='brute'",
            ("import sys\nm = int(sys.stdin.readline())\nprint(m)\n",),
        )
        self.assertTrue(any("解法只有 1 种" in item for item in self.issues()))

    # ── 逐行解析 ────────────────────────────────────────────────────────────
    def test_legacy_prose_notes_fail(self) -> None:
        self.db.execute(
            "UPDATE reference_solutions SET line_notes_json=? WHERE role='alt1'",
            ("先读入，再二分查找，最后输出。",),
        )
        self.assertTrue(any("逐行代码解析 1/2 份" in item for item in self.issues()))

    def test_low_coverage_notes_fail(self) -> None:
        # 结构是 v3，但只讲了一半非空行 —— 和"只讲 3 行糊弄"等价
        self.db.execute(
            "UPDATE reference_solutions SET line_notes_json=? WHERE role='alt1'",
            (_notes(rows=6, coverage="half"),),
        )
        self.assertTrue(any("逐行代码解析 1/2 份" in item for item in self.issues()))

    def test_notes_on_brute_are_not_required(self) -> None:
        self.assertEqual(self.issues(), [])

    # ── 提示 ────────────────────────────────────────────────────────────────
    def test_missing_root_hint_fails(self) -> None:
        self.db.execute("DELETE FROM hints WHERE level=0")
        self.assertTrue(any("缺 L1" in item for item in self.issues()))

    def test_missing_per_method_hint_fails(self) -> None:
        self.db.execute("DELETE FROM hints WHERE approach='二分' AND level=3")
        self.assertTrue(any("提示缺 L2/L3：二分" in item for item in self.issues()))

    # ── 数据 ────────────────────────────────────────────────────────────────
    def test_insufficient_cases_fail(self) -> None:
        self.db.execute("DELETE FROM test_cases WHERE layer='exhaustive' AND ordinal>=3")
        issues = self.issues()
        self.assertTrue(any("非样例数据" in item and "≥10" in item for item in issues), issues)

    def test_missing_layer_fails(self) -> None:
        self.db.execute("DELETE FROM test_cases WHERE layer='boundary'")
        self.assertTrue(any("缺少分层数据：boundary" in item for item in self.issues()))

    def test_sample_layer_does_not_count_toward_minimum(self) -> None:
        self.db.executemany(
            "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
            " expected_output, source_of_truth, trust_level)"
            " VALUES(1, 'sample', ?, ?, ?, 'e', 'sample', 'oracle')",
            [(i, f"s{i}", f"sample{i}") for i in range(30)],
        )
        self.assertEqual(self.issues(), [])

    # ── 题解 ────────────────────────────────────────────────────────────────
    def test_missing_thinking_path_fails(self) -> None:
        self.db.execute(
            "UPDATE problems SET analysis_json=? WHERE id=1",
            (json.dumps({"plain_question": "q"}),),
        )
        self.assertTrue(any("thinking_path" in item for item in self.issues()))

    def test_broken_analysis_json_fails(self) -> None:
        self.db.execute("UPDATE problems SET analysis_json='{not json' WHERE id=1")
        self.assertTrue(any("plain_question" in item for item in self.issues()))


class PrepStampsVersionTest(unittest.TestCase):
    """盖章必须发生在产物门之后：门不过 → 版本号绝不写进去。"""

    def test_gate_runs_before_stamp(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "prep_service.py").read_text(
            encoding="utf-8"
        )
        gate_at = source.index("STAGE_ARTIFACTS, artifact_gate.gate_stage")
        stamp_at = source.index("artifact_version=ARTIFACT_VERSION")
        self.assertLess(gate_at, stamp_at)


if __name__ == "__main__":
    unittest.main()
