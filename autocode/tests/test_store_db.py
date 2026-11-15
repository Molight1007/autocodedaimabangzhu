"""存储层测试：PRAGMA、建表、去重、事务回滚、级联删除、参数化标识符。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autocode.store.db import Database, _safe_identifier


class TestDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_pragmas(self) -> None:
        self.assertEqual(str(self.db.query_scalar("PRAGMA journal_mode")).lower(), "wal")
        self.assertEqual(int(self.db.query_scalar("PRAGMA foreign_keys")), 1)
        self.assertEqual(int(self.db.query_scalar("PRAGMA busy_timeout")), 5000)

    def test_all_tables_created(self) -> None:
        counts = self.db.table_counts()
        for table in (
            "problems",
            "test_cases",
            "reference_solutions",
            "generators",
            "hints",
            "study_records",
            "run_results",
            "problem_stage_runs",
            "problem_progress",
            "problem_drafts",
            "similar_problems",
            "schema_migrations",
        ):
            self.assertIn(table, counts)

    def test_schema_migration_recorded(self) -> None:
        self.assertEqual(int(self.db.query_scalar("SELECT COUNT(*) FROM schema_migrations")), 1)

    def test_statement_key_dedupes(self) -> None:
        sql = (
            "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
            " VALUES(?, ?, 'draft', 'now', 'now')"
        )
        self.db.execute(sql, ("A", "同样的题面" * 50))
        with self.assertRaises(Exception):
            self.db.execute(sql, ("B", "同样的题面" * 50))

    def test_foreign_key_cascade(self) -> None:
        self.db.execute(
            "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
            " VALUES('A', 'x', 'draft', 'now', 'now')"
        )
        problem_id = int(self.db.query_scalar("SELECT id FROM problems"))
        self.db.execute(
            "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash)"
            " VALUES(?, 'sample', 1, '1', 'h1')",
            (problem_id,),
        )
        self.db.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
        self.assertEqual(int(self.db.query_scalar("SELECT COUNT(*) FROM test_cases")), 0)

    def test_bad_language_value_rejected_by_check(self) -> None:
        self.db.execute(
            "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
            " VALUES('A', 'x', 'draft', 'now', 'now')"
        )
        problem_id = int(self.db.query_scalar("SELECT id FROM problems"))
        with self.assertRaises(Exception):
            self.db.execute(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " source_of_truth) VALUES(?, 'sample', 1, '1', 'h2', 'llm_guess')",
                (problem_id,),
            )

    def test_transaction_rollback(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
                    " VALUES('T', 'y', 'draft', 'now', 'now')"
                )
                raise RuntimeError("boom")
        self.assertEqual(int(self.db.query_scalar("SELECT COUNT(*) FROM problems")), 0)

    def test_repeat_init_is_idempotent(self) -> None:
        self.db.init_schema()
        self.assertEqual(int(self.db.query_scalar("SELECT COUNT(*) FROM schema_migrations")), 1)


class TestIdentifierGuard(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(_safe_identifier("test_cases"), "test_cases")

    def test_injection_rejected(self) -> None:
        for bad in ("test_cases; DROP TABLE problems", "1abc", "", "a b"):
            with self.assertRaises(ValueError):
                _safe_identifier(bad)


if __name__ == "__main__":
    unittest.main()
