"""学生账号登录：学号能登、错密码拒、停用拒、管理员兜底、限流只锁本人。

为什么要有这组测试（2026-11-22）：
上线前登录只认一组凭证（`load_credentials`），175 个学生一次都登不进去。
改成「先查 student_accounts，再回落管理员」之后，两条路径必须同时成立——
学生能进，运维账号也不能被锁在门外。限流也从「按 IP 算人头」改成「按账号算」，
否则同一出口 NAT 后面的班级会被第一个打错密码的人一起锁死。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autocode.slim2 import auth, config
from autocode.store.db import Database

ADMIN_USER = "autocode-test"
ADMIN_PASS = "AdminPass-2026"

STUDENTS = (
    ("202600000001", "张三", "1班", "zhangsan", "active"),
    ("202600000002", "李四", "1班", "lisi", "active"),
    ("202600000003", "王五", "2班", "wangwu", "disabled"),
)


def _password(sid: str, pinyin: str) -> str:
    return sid + pinyin


class TestStudentLogin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        now = auth.iso(auth.utcnow())
        with self.db.transaction() as conn:
            for sid, name, cls, pinyin, status in STUDENTS:
                conn.execute(
                    "INSERT INTO student_accounts(student_no,name,class_name,pinyin,password_hash,"
                    "status,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sid, name, cls, pinyin, auth.hash_password(_password(sid, pinyin)), status, now, now),
                )
        # 管理员凭证走环境变量，避免 load_credentials 在 data/ 下生成文件
        self._env = mock.patch.dict(
            os.environ,
            {
                "AUTOCODE_SLIM2_USER": ADMIN_USER,
                "AUTOCODE_SLIM2_PASSWORD_HASH": auth.hash_password(ADMIN_PASS),
            },
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.db.close()
        self._tmp.cleanup()

    # ── 身份校验 ────────────────────────────────────────────────────────────
    def test_student_can_login(self) -> None:
        actor = auth.authenticate(self.db, "202600000001", "202600000001zhangsan")
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertEqual(actor["user"], "202600000001")
        self.assertEqual(actor["name"], "张三")
        self.assertEqual(actor["role"], "student")

    def test_wrong_password_rejected(self) -> None:
        self.assertIsNone(auth.authenticate(self.db, "202600000001", "202600000001zhangsanX"))
        self.assertIsNone(auth.authenticate(self.db, "202600000001", ""))

    def test_disabled_account_rejected(self) -> None:
        # 密码是对的，但账号被停用——必须拒绝，否则「停用」形同虚设
        self.assertIsNone(auth.authenticate(self.db, "202600000003", "202600000003wangwu"))

    def test_unknown_user_rejected(self) -> None:
        self.assertIsNone(auth.authenticate(self.db, "202600009999", "whatever"))

    def test_admin_still_works(self) -> None:
        actor = auth.authenticate(self.db, ADMIN_USER, ADMIN_PASS)
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertEqual(actor["role"], "admin")

    def test_same_permission_shape(self) -> None:
        """两类账号权限相同：返回结构一致，只差 role。"""
        student = auth.authenticate(self.db, "202600000001", "202600000001zhangsan")
        admin = auth.authenticate(self.db, ADMIN_USER, ADMIN_PASS)
        assert student is not None and admin is not None
        self.assertEqual(set(student), set(admin))

    def test_touch_login_writes_timestamp(self) -> None:
        row = self.db.query_one(
            "SELECT last_login_at FROM student_accounts WHERE student_no=?", ("202600000001",)
        )
        self.assertIsNone(row["last_login_at"])
        auth.touch_login(self.db, "202600000001")
        row = self.db.query_one(
            "SELECT last_login_at FROM student_accounts WHERE student_no=?", ("202600000001",)
        )
        self.assertTrue(row["last_login_at"])


class TestLoginLimiter(unittest.TestCase):
    def test_locks_only_that_account(self) -> None:
        limiter = auth.LoginLimiter(max_fails=5, window_sec=300, lock_sec=600, ip_max=200)
        ip = "10.0.0.9"  # 全班共用的 NAT 出口
        for _ in range(5):
            limiter.record_failure(ip, "202600000001")
        self.assertGreater(limiter.retry_after(ip, "202600000001"), 0)
        # 同 IP 的另一位同学不受影响——这是关键，教室 NAT 下不能连坐
        self.assertEqual(limiter.retry_after(ip, "202600000002"), 0)

    def test_ip_flood_still_capped(self) -> None:
        # ip_max 会被抬到 max_fails，避免「一个人错 5 次就锁住整个出口」
        limiter = auth.LoginLimiter(max_fails=2, window_sec=300, lock_sec=600, ip_max=4)
        self.assertEqual(limiter.ip_max, 4)
        for i in range(4):
            limiter.record_failure("10.0.0.9", f"2026000000{i:02d}")
        self.assertGreater(limiter.retry_after("10.0.0.9", "202600000004"), 0)
        self.assertEqual(limiter.retry_after("10.0.0.10", "202600000004"), 0)

    def test_ip_max_never_below_account_max(self) -> None:
        limiter = auth.LoginLimiter(max_fails=5, window_sec=300, lock_sec=600, ip_max=1)
        self.assertEqual(limiter.ip_max, 5)

    def test_soft_lock_then_hard_lock(self) -> None:
        """5 次只锁 60 秒（防整人），累计 10 次才升级成 10 分钟。"""
        limiter = auth.LoginLimiter(max_fails=5, window_sec=300, lock_sec=600, ip_max=200)
        ip = "10.0.0.9"
        for _ in range(4):
            limiter.record_failure(ip, "202600000001")
        self.assertEqual(limiter.retry_after(ip, "202600000001"), 0)
        limiter.record_failure(ip, "202600000001")
        soft = limiter.retry_after(ip, "202600000001")
        self.assertGreater(soft, 0)
        self.assertLessEqual(soft, 60)
        for _ in range(5):
            limiter.record_failure(ip, "202600000001")
        self.assertGreater(limiter.retry_after(ip, "202600000001"), 60)

    def test_reset_clears_account_and_ip(self) -> None:
        limiter = auth.LoginLimiter(max_fails=2, window_sec=300, lock_sec=600, ip_max=200)
        limiter.record_failure("10.0.0.9", "202600000001")
        limiter.record_failure("10.0.0.9", "202600000001")
        self.assertGreater(limiter.retry_after("10.0.0.9", "202600000001"), 0)
        limiter.reset("10.0.0.9", "202600000001")
        self.assertEqual(limiter.retry_after("10.0.0.9", "202600000001"), 0)


if __name__ == "__main__":
    unittest.main()
