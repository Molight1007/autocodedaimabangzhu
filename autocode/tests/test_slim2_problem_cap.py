"""题库上限：导入后只保留最新 100 道。

规则（2026-11-23 用户定）：整个题库最多 100 道；每次确认入库都先看数量，
超出的按入库顺序从最早的删起，删除不记清单。删一道题必须连同它的
测试数据、对抗数据、任务与插图一起清掉，否则会留下孤儿行和磁盘垃圾。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autocode.slim2 import api, config, problem_purge, segment
from autocode.store.db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestProblemCap(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db = Database(self.root / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        # 插图目录也指到临时目录，避免测试碰到真实的 data/assets
        self._data_patch = patch.object(config, "DATA_DIR", self.root)
        self._data_patch.start()

    def tearDown(self) -> None:
        self._data_patch.stop()
        self.db.close()
        self._tmp.cleanup()

    def _problem(self, title: str) -> int:
        now = _now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, statement_hash, created_at, updated_at)"
                " VALUES(?,?,?,?,?)",
                (title, title, f"hash-{title}", now, now),
            )
            return int(cursor.lastrowid)

    def test_trim_keeps_newest_100(self) -> None:
        ids = [self._problem(f"p{index}") for index in range(105)]
        self.assertEqual(problem_purge.trim_to_cap(self.db), 5)
        left = [int(row["id"]) for row in self.db.query_all("SELECT id FROM problems ORDER BY id")]
        self.assertEqual(left, ids[5:])

    def test_trim_does_nothing_below_cap(self) -> None:
        self._problem("only")
        self.assertEqual(problem_purge.trim_to_cap(self.db), 0)

    def test_purge_removes_dependent_rows_and_assets(self) -> None:
        pid = self._problem("t")
        now = _now()
        with self.db.transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO test_cases(problem_id, version, layer, input_data, input_hash,"
                " expected_output, created_at) VALUES(?, 1, 'sample', '1', 'h1', '1', ?)",
                (pid, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO case_reviews(case_id, verdict, created_at) VALUES(?, 'pass', ?)",
                (case_id, now),
            )
            submission = conn.execute(
                "INSERT INTO submissions(problem_id, lang, code, code_hash, created_at)"
                " VALUES(?, 'py', 'x', 'c1', ?)",
                (pid, now),
            ).lastrowid
            adv_id = conn.execute(
                "INSERT INTO adversarial_cases(problem_id, submission_id, input_data, input_hash,"
                " expected_output, created_at) VALUES(?, ?, '1', 'a1', '1', ?)",
                (pid, submission, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO adversarial_reviews(adv_case_id, verdict, created_at)"
                " VALUES(?, 'accept', ?)",
                (adv_id, now),
            )
            conn.execute(
                "INSERT INTO jobs(problem_id, kind, status, started_at)"
                " VALUES(?, 'dataset', 'done', ?)",
                (pid, now),
            )
            conn.execute(
                "INSERT INTO problem_figures(problem_id, label, page, path, created_at)"
                " VALUES(?, 'fig_1', 1, '/x.png', ?)",
                (pid, now),
            )
        assets = config.DATA_DIR / "assets" / f"problem_{pid}"
        assets.mkdir(parents=True)
        (assets / "fig_1.png").write_bytes(b"x")

        problem_purge.purge_problem(self.db, pid)

        for table in (
            "test_cases", "case_reviews", "submissions", "adversarial_cases",
            "adversarial_reviews", "jobs", "problem_figures", "problems",
        ):
            self.assertEqual(
                self.db.query_scalar(f"SELECT COUNT(*) FROM {table}", (), 0), 0, table
            )
        self.assertFalse(assets.exists())

    def test_confirm_import_drops_oldest_when_full(self) -> None:
        self._problem("placeholder")  # 先占住 id=1，后面补到 100 道
        for index in range(99):
            self._problem(f"old{index}")
        now = _now()
        with self.db.transaction() as conn:
            batch_id = conn.execute(
                "INSERT INTO import_batches(source_name, kind, raw_text, status, created_at)"
                " VALUES('t', 'text', 'raw', 'segmented', ?)",
                (now,),
            ).lastrowid
            conn.execute(
                "INSERT INTO import_segments(batch_id, idx, title, start_line, end_line,"
                " statement_md, created_at) VALUES(?, 1, '新题', 1, 2, '一道新题的题面', ?)",
                (batch_id, now),
            )

        result = segment.confirm_import(self.db, int(batch_id), [1])

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["library_total"], config.MAX_PROBLEMS_TOTAL)
        self.assertEqual(result["library_replaced"], 1)
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM problems", (), 0),
            config.MAX_PROBLEMS_TOTAL,
        )
        titles = [row["title"] for row in self.db.query_all("SELECT title FROM problems")]
        self.assertIn("新题", titles)
        self.assertNotIn("placeholder", titles)  # 最早入库的那道被清掉
        self.assertEqual(api.list_problems(self.db)["cap"], config.MAX_PROBLEMS_TOTAL)

    def test_removed_problem_cannot_be_used(self) -> None:
        """被顶掉的题不能留下任何可用入口：详情、数据、生成、导出都必须是"不存在"。"""
        pid = self._problem("gone")
        problem_purge.purge_problem(self.db, pid)

        with self.assertRaises(KeyError):
            api.problem_detail(self.db, pid)
        with self.assertRaises(KeyError):
            api.list_cases(self.db, pid)
        with self.assertRaises(KeyError):
            api.list_adversarial(self.db, pid)
        self.assertEqual(api.start_dataset(self.db, {"id": pid})["code"], "NOT_FOUND")
        self.assertEqual(
            api.start_adversarial(self.db, {"id": pid, "code": "x", "lang": "python"})["code"],
            "NOT_FOUND",
        )
        self.assertEqual(api.export_problem(self.db, pid)["code"], "NOT_FOUND")
