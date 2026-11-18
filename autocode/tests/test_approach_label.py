"""算法标签回填测试：命名要短、要像算法名，回填要覆盖三处。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autocode.app.approach_label import normalize_name, relabel_problem
from autocode.store.db import Database


class TestNormalizeName(unittest.TestCase):
    def test_accepts_short_names(self) -> None:
        for raw in ("试除法", "状压DP", "BFS", "前后缀最值"):
            self.assertEqual(normalize_name(raw), raw)

    def test_rejects_sentences_and_placeholders(self) -> None:
        self.assertIsNone(normalize_name("这道题可以用试除法解决"))
        self.assertIsNone(normalize_name("未标注方法"))
        self.assertIsNone(normalize_name("解法一"))
        self.assertIsNone(normalize_name(""))
        self.assertIsNone(normalize_name("一" * 20))


class TestRelabel(unittest.TestCase):
    def test_updates_solutions_analysis_and_hints(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "l.db")
            db.init_schema()
            db.execute(
                "INSERT INTO problems(id,title,statement_md,status,analysis_json,created_at,updated_at)"
                " VALUES(1,'题','面','draft',?,'now','now')",
                (json.dumps({"approaches": [{"name": "未标注方法", "complexity": "O(n)"}]},
                            ensure_ascii=False),),
            )
            for lang, role in (("python", "reference"), ("cpp", "reference"), ("python", "brute")):
                db.execute(
                    "INSERT INTO reference_solutions(problem_id,lang,role,code,verified,approach)"
                    " VALUES(1,?,?,'print(1)',1,NULL)",
                    (lang, role),
                )
            for level in (2, 3, 4):
                db.execute(
                    "INSERT INTO hints(problem_id,level,content,gen_batch_id,approach,is_active)"
                    " VALUES(1,?,?, 'b','未标注方法',1)",
                    (level, "步骤"),
                )
            counts = relabel_problem(db, 1, "试除法")
            self.assertEqual(counts, {"solutions": 3, "analysis": 1, "hints": 3})
            rows = db.query_all("SELECT role,approach FROM reference_solutions ORDER BY role")
            self.assertEqual({r["approach"] for r in rows if r["role"] != "brute"}, {"试除法"})
            self.assertEqual({r["approach"] for r in rows if r["role"] == "brute"}, {"暴力枚举"})
            analysis = json.loads(db.query_one("SELECT analysis_json FROM problems")["analysis_json"])
            self.assertEqual(analysis["approaches"][0]["name"], "试除法")
            self.assertEqual(
                db.query_scalar("SELECT COUNT(*) FROM hints WHERE approach='未标注方法'", (), 0), 0
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
