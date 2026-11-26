"""导入时自动难度评估（2026-11-26）：easy / medium / hard。

三条契约：
1. 评估是纯规则、不花模型额度、任何输入不抛异常（失败留空，不拦导入）；
2. confirm_import 落库那一刻就带上 difficulty（列表"待生成"栏直接显示颜色）；
3. 老库启动时对 difficulty='' 的题回填一次。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autocode.slim2 import config, segment
from autocode.slim2.difficulty import NAMES, estimate
from autocode.slim2.db import _backfill_difficulty
from autocode.store.db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestEstimate(unittest.TestCase):
    def test_levels_and_names_consistent(self) -> None:
        for level in ("easy", "medium", "hard"):
            self.assertIn(level, NAMES)
            self.assertTrue(NAMES[level])

    def test_plain_sum_is_easy(self) -> None:
        self.assertEqual(estimate("输入两个整数 a 和 b，输出它们的和。"), "easy")

    def test_small_scope_simulation_is_easy(self) -> None:
        self.assertEqual(estimate("模拟这个过程，n ≤ 100，输出统计结果。"), "easy")

    def test_greedy_medium(self) -> None:
        self.assertEqual(estimate("采用贪心策略选择最优方案，n ≤ 1e5。"), "medium")

    def test_dp_hard(self) -> None:
        self.assertEqual(
            estimate("使用动态规划求解最长上升子序列，1 ≤ n ≤ 1e5。"), "hard"
        )

    def test_factorization_with_superscript_hard(self) -> None:
        # normalize_math 后题面用上标字形（10¹⁰），规模抓取必须认得上标
        self.assertEqual(estimate("对正整数 x 进行质因数分解，1 ≤ x ≤ 10¹⁰。"), "hard")

    def test_constraints_contribute(self) -> None:
        # 题面本身无信号，数据范围写在约束里
        self.assertEqual(
            estimate("按题目要求输出结果。", ["1 ≤ n ≤ 10^9", "答案对 998244353 取模"]),
            "medium",
        )

    def test_special_judge_bumps_difficulty(self) -> None:
        base = "输出一个实数，与标准答案误差在允许范围内即视为正确。"
        self.assertEqual(estimate(base), "easy")
        self.assertEqual(estimate(base, judge_type="special"), "medium")

    def test_never_raises(self) -> None:
        # 各种脏输入都返回合法档位或空串，绝不抛异常
        for statement in ("", None, 123, "x" * 10000):
            level = estimate(statement)  # type: ignore[arg-type]
            self.assertIn(level, ("", "easy", "medium", "hard"))


class TestConfirmImportStoresDifficulty(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db = Database(self.root / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _segment(self, statement: str) -> int:
        now = _now()
        with self.db.transaction() as conn:
            batch_id = conn.execute(
                "INSERT INTO import_batches(source_name, kind, raw_text, status, created_at)"
                " VALUES('t', 'text', 'raw', 'segmented', ?)",
                (now,),
            ).lastrowid
            conn.execute(
                "INSERT INTO import_segments(batch_id, idx, title, start_line, end_line,"
                " statement_md, created_at) VALUES(?, 1, '难度测试题', 1, 2, ?, ?)",
                (batch_id, statement, now),
            )
            return int(batch_id)

    def test_difficulty_written_on_confirm(self) -> None:
        batch_id = self._segment("使用动态规划求解最长上升子序列，1 ≤ n ≤ 1e5。")
        result = segment.confirm_import(self.db, batch_id, [1])
        self.assertTrue(result["ok"])
        row = self.db.query_one(
            "SELECT difficulty FROM problems WHERE id=?", (result["imported"][0]["problem_id"],)
        )
        self.assertEqual(str(row["difficulty"]), "hard")


class TestBackfill(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db = Database(self.root / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _problem(self, statement: str, difficulty: str = "") -> int:
        now = _now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, statement_hash, difficulty,"
                " constraints_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                ("t", statement, f"h-{statement[:8]}", difficulty,
                 json.dumps(["1 ≤ n ≤ 1e5"]), now, now),
            )
            return int(cursor.lastrowid)

    def test_backfills_empty_rows_only(self) -> None:
        old_id = self._problem("使用动态规划求解，1 ≤ n ≤ 1e5。")          # 老题：空 → 回填
        filled_id = self._problem("输入两个整数 a 和 b，输出它们的和。", "easy")  # 已有值：不动
        conn = self.db.connect()
        _backfill_difficulty(conn)
        self.assertEqual(
            str(self.db.query_one("SELECT difficulty FROM problems WHERE id=?", (old_id,))["difficulty"]),
            "hard",
        )
        self.assertEqual(
            str(self.db.query_one("SELECT difficulty FROM problems WHERE id=?", (filled_id,))["difficulty"]),
            "easy",
        )
        # 幂等：再跑一次不报错、不改变结果
        _backfill_difficulty(conn)
        self.assertEqual(
            str(self.db.query_one("SELECT difficulty FROM problems WHERE id=?", (old_id,))["difficulty"]),
            "hard",
        )


if __name__ == "__main__":
    unittest.main()
