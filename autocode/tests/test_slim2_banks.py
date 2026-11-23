"""题库权限：默认题库全员可改，永久库只有管理员能增删、且不设上限。

规则（2026-11-23 用户定）：
1. 管理员可新建永久库；学生建库必须被拒；
2. 永久库里的题：学生既不能加也不能删，管理员可以；
3. 永久库不限数量，任何入库/清理都不会自动删题；
4. 默认题库保持原样：100 上限、所有账号可增删；
5. 同一道题可以在不同库里各存一份（唯一键按库算）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autocode.slim2 import api, banks, config, problem_purge, segment
from autocode.store.db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(unittest.TestCase):
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

    def _problem(self, title: str, bank_id: int = banks.DEFAULT_ID, digest: str | None = None) -> int:
        now = _now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (int(bank_id), title, title, digest or f"hash-{title}", now, now),
            )
            return int(cursor.lastrowid)

    def _batch(self, statement: str) -> int:
        now = _now()
        with self.db.transaction() as conn:
            batch_id = conn.execute(
                "INSERT INTO import_batches(source_name, kind, raw_text, status, created_at)"
                " VALUES('t', 'text', 'raw', 'segmented', ?)",
                (now,),
            ).lastrowid
            conn.execute(
                "INSERT INTO import_segments(batch_id, idx, title, start_line, end_line,"
                " statement_md, created_at) VALUES(?, 1, '新题', 1, 2, ?, ?)",
                (batch_id, statement, now),
            )
        return int(batch_id)

    def _bank(self, name: str) -> int:
        result = api.create_bank(self.db, {"name": name}, "admin", "autocode")
        self.assertTrue(result.get("ok"), result)
        return int(result["bank"]["id"])


class TestDefaultBank(Base):
    def test_default_bank_exists_and_is_temp(self) -> None:
        bank = banks.get(self.db, banks.DEFAULT_ID)
        self.assertIsNotNone(bank)
        self.assertEqual(bank["kind"], banks.KIND_TEMP)
        self.assertEqual(banks.cap_of(bank), config.MAX_PROBLEMS_TOTAL)
        # 学生也能往默认题库加题、删题——这是现有流程，不许被这次改动收窄
        self.assertTrue(banks.can_manage(bank, "student"))

    def test_list_problems_filters_by_bank(self) -> None:
        other = self._bank("另一个永久库")
        self._problem("默认题")
        self._problem("永久题", bank_id=other)
        self.assertEqual(api.list_problems(self.db)["total"], 1)
        listed = api.list_problems(self.db, bank_id=other)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["cap"], 0)
        self.assertEqual(listed["items"][0]["title"], "永久题")

    def test_unknown_bank_is_404(self) -> None:
        with self.assertRaises(KeyError):
            api.list_problems(self.db, bank_id=999)


class TestCreateBank(Base):
    def test_admin_can_create_permanent_bank(self) -> None:
        result = api.create_bank(self.db, {"name": "CCF-CSP 历届第一题"}, "admin", "autocode")
        self.assertTrue(result["ok"])
        bank = result["bank"]
        self.assertEqual(bank["kind"], banks.KIND_PERMANENT)
        self.assertEqual(bank["cap"], 0)
        self.assertTrue(bank["can_manage"])  # 建库返回的是管理员视角
        # 学生视角不能管理
        student_view = banks.view(self.db, banks.get(self.db, bank["id"]), "student")
        self.assertFalse(student_view["can_manage"])

    def test_student_cannot_create_bank(self) -> None:
        result = api.create_bank(self.db, {"name": "学生私库"}, "student", "202508070103")
        self.assertEqual(result["code"], "FORBIDDEN")
        self.assertEqual(self.db.query_scalar("SELECT COUNT(*) FROM banks", (), 0), 1)

    def test_name_validation(self) -> None:
        self.assertEqual(api.create_bank(self.db, {"name": "  "}, "admin")["code"], "INVALID_INPUT")
        self.assertEqual(
            api.create_bank(self.db, {"name": "x" * (banks.MAX_NAME_CHARS + 1)}, "admin")["code"],
            "INVALID_INPUT",
        )
        self.assertEqual(
            api.create_bank(self.db, {"name": banks.DEFAULT_NAME}, "admin")["code"],
            "INVALID_INPUT",
        )
        self.assertTrue(api.create_bank(self.db, {"name": "A"}, "admin")["ok"])
        self.assertEqual(
            api.create_bank(self.db, {"name": "A"}, "admin")["code"], "INVALID_INPUT"
        )


class TestPermanentPermissions(Base):
    def test_student_cannot_import_into_permanent(self) -> None:
        bank_id = self._bank("永久库")
        batch_id = self._batch("一道全新的题面")
        result = segment.confirm_import(
            self.db, batch_id, [1], bank_id=bank_id, role="student"
        )
        self.assertEqual(result["code"], "FORBIDDEN")
        self.assertEqual(
            self.db.query_scalar(
                "SELECT COUNT(*) FROM problems WHERE bank_id=?", (bank_id,), 0
            ),
            0,
        )

    def test_student_cannot_even_segment_for_permanent(self) -> None:
        bank_id = self._bank("永久库")
        # 不传 runner：权限检查必须在模型调用之前，拿到明确的拒绝而不是 AttributeError
        result = segment.segment_text(
            self.db, None, "x" * 500, bank_id=bank_id, role="student"
        )
        self.assertEqual(result["code"], "FORBIDDEN")

    def test_admin_can_import_into_permanent(self) -> None:
        bank_id = self._bank("永久库")
        batch_id = self._batch("一道全新的题面")
        result = segment.confirm_import(
            self.db, batch_id, [1], bank_id=bank_id, role="admin"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["bank"]["kind"], banks.KIND_PERMANENT)
        self.assertEqual(result["library_cap"], 0)
        self.assertEqual(result["library_replaced"], 0)
        self.assertEqual(
            self.db.query_scalar(
                "SELECT bank_id FROM problems WHERE id=?",
                (result["imported"][0]["problem_id"],),
                0,
            ),
            bank_id,
        )

    def test_student_cannot_delete_permanent_problem(self) -> None:
        bank_id = self._bank("永久库")
        pid = self._problem("永久题", bank_id=bank_id)
        result = api.delete_problem(self.db, pid, "", "student")
        self.assertEqual(result["code"], "FORBIDDEN")
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM problems WHERE id=?", (pid,), 0), 1
        )
        self.assertTrue(api.delete_problem(self.db, pid, "", "admin")["ok"])

    def test_student_can_still_delete_default_problem(self) -> None:
        pid = self._problem("默认题")
        self.assertTrue(api.delete_problem(self.db, pid, "", "student")["ok"])

    def test_missing_bank_rejected(self) -> None:
        batch_id = self._batch("题面")
        result = segment.confirm_import(self.db, batch_id, [1], bank_id=999, role="admin")
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertEqual(api.bank_write_error(self.db, 999, "admin")["code"], "NOT_FOUND")


class TestPermanentHasNoCap(Base):
    def test_permanent_bank_is_never_trimmed(self) -> None:
        bank_id = self._bank("永久库")
        for index in range(config.MAX_PROBLEMS_TOTAL + 5):
            self._problem(f"p{index}", bank_id=bank_id)
        self.assertEqual(problem_purge.trim_to_cap(self.db, bank_id=bank_id), 0)
        self.assertEqual(
            self.db.query_scalar(
                "SELECT COUNT(*) FROM problems WHERE bank_id=?", (bank_id,), 0
            ),
            config.MAX_PROBLEMS_TOTAL + 5,
        )

    def test_temp_bank_still_trimmed(self) -> None:
        for index in range(config.MAX_PROBLEMS_TOTAL + 5):
            self._problem(f"p{index}")
        self.assertEqual(
            problem_purge.trim_to_cap(self.db, bank_id=banks.DEFAULT_ID), 5
        )
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM problems", (), 0),
            config.MAX_PROBLEMS_TOTAL,
        )

    def test_same_statement_allowed_in_two_banks(self) -> None:
        bank_id = self._bank("永久库")
        self._problem("同题", digest="same-hash")
        # 不同库各自一份：不触发唯一键冲突
        self._problem("同题", bank_id=bank_id, digest="same-hash")
        self.assertEqual(self.db.query_scalar("SELECT COUNT(*) FROM problems", (), 0), 2)


if __name__ == "__main__":
    unittest.main()
