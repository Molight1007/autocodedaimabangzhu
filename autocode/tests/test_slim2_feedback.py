"""问题反馈：学生各看各的，管理员看全部并回复，别人不能插嘴。

为什么要有这组测试（2026-11-22 用户要求）：
工具给 175 个学生共用，遇到问题只能线下喊，改进意见也没地方沉淀。
做成页面里的反馈后，权限必须卡死——
学生 A 不能看到 B 提的问题（里面有学号姓名），更不能替别人回复；
只有管理员能回复全部，回复后状态变「已回复」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autocode.slim2 import api, auth, config
from autocode.store.db import Database

ADMIN = "autocode-test"
STU_A = "202600000001"
STU_B = "202600000002"


class TestFeedback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        self._env = mock.patch.dict(
            os.environ,
            {
                "AUTOCODE_SLIM2_USER": ADMIN,
                "AUTOCODE_SLIM2_PASSWORD_HASH": auth.hash_password("AdminPass-2026"),
            },
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _create(self, user: str, title: str = "第 3 题生成失败") -> int:
        result = api.create_feedback(
            self.db, user, auth.display_name(self.db, user),
            {"kind": "bug", "title": title, "body": "点生成后一直转圈，十分钟没有结果。"},
        )
        self.assertTrue(result.get("ok"), result)
        return int(result["id"])

    def test_roles(self) -> None:
        self.assertEqual(auth.role_of(ADMIN), "admin")
        self.assertEqual(auth.role_of(STU_A), "student")

    def test_student_sees_only_own(self) -> None:
        self._create(STU_A)
        self._create(STU_B, "希望支持导出 Excel")
        own = api.list_feedback(self.db, STU_A, "student")
        self.assertEqual(own["total"], 1)
        self.assertEqual(own["items"][0]["user"], STU_A)

        everything = api.list_feedback(self.db, ADMIN, "admin")
        self.assertEqual(everything["total"], 2)
        self.assertEqual(everything["stats"]["open"], 2)

    def test_admin_reply_marks_answered(self) -> None:
        fid = self._create(STU_A)
        result = api.reply_feedback(self.db, fid, ADMIN, "admin", {"body": "已修复，刷新后重试。"})
        self.assertTrue(result.get("ok"), result)

        row = self.db.query_one("SELECT status FROM feedback WHERE id=?", (fid,))
        self.assertEqual(row["status"], "answered")
        listed = api.list_feedback(self.db, STU_A, "student")
        self.assertEqual(len(listed["items"][0]["replies"]), 1)
        self.assertEqual(listed["items"][0]["replies"][0]["role"], "admin")

    def test_other_student_cannot_reply(self) -> None:
        fid = self._create(STU_A)
        result = api.reply_feedback(self.db, fid, STU_B, "student", {"body": "我也遇到了"})
        self.assertEqual(result.get("code"), "FORBIDDEN")
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM feedback_replies", (), 0), 0
        )

    def test_owner_can_follow_up_and_close(self) -> None:
        fid = self._create(STU_A)
        self.assertTrue(api.reply_feedback(self.db, fid, STU_A, "student", {"body": "补充：第 2 条也失败"})["ok"])
        self.assertTrue(api.close_feedback(self.db, fid, STU_A, "student")["ok"])
        row = self.db.query_one("SELECT status FROM feedback WHERE id=?", (fid,))
        self.assertEqual(row["status"], "closed")
        blocked = api.reply_feedback(self.db, fid, STU_A, "student", {"body": "再补一句"})
        self.assertEqual(blocked.get("code"), "CLOSED")

    def test_validation(self) -> None:
        short = api.create_feedback(self.db, STU_A, "", {"title": "坏了", "body": "打不开"})
        self.assertEqual(short.get("code"), "INVALID_INPUT")
        no_body = api.create_feedback(self.db, STU_A, "", {"title": "题目生成一直失败", "body": ""})
        self.assertEqual(no_body.get("code"), "INVALID_INPUT")

    def test_close_permission(self) -> None:
        fid = self._create(STU_A)
        result = api.close_feedback(self.db, fid, STU_B, "student")
        self.assertEqual(result.get("code"), "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
