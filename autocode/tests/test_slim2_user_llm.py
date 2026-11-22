"""每人一份 API Key：互相看不到、互相用不了、Key 加密落库、Base URL 不许打内网。

为什么要有这组测试（2026-11-22 用户要求）：
原来全站共用一份 Key，谁登录都能花老师的额度，也能在设置页看到同一份掩码。
改成「一个账号一行」之后，三条必须同时成立——
1. A 存的 Key，B 查不到、也用不上；
2. 库里不是明文，拷走库文件也还原不出 Key；
3. 填 `https://127.0.0.1/...` 这类内网地址要被拒，否则服务器会被当跳板。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autocode.slim2 import config, secretbox, user_llm
from autocode.store.db import Database

KEY_A = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "sk-bbbbbbbbbbbbbbbbbbbbbbbbbbbb2222"


class TestSecretBox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {"AUTOCODE_SLIM2_MASTER_KEY_FILE": str(Path(self._tmp.name) / "master.key")},
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        token = secretbox.seal(KEY_A)
        self.assertNotIn(KEY_A, token)
        self.assertEqual(secretbox.unseal(token), KEY_A)

    def test_tamper_detected(self) -> None:
        token = secretbox.seal(KEY_A)
        head, nonce, body, tag = token.split(":")
        flipped = body[:-4] + ("AAAA" if body[-4:] != "AAAA" else "BBBB")
        self.assertEqual(secretbox.unseal(":".join([head, nonce, flipped, tag])), "")

    def test_wrong_master_key_cannot_open(self) -> None:
        token = secretbox.seal(KEY_A)
        Path(Path(self._tmp.name) / "master.key").write_bytes(b"x" * 32)
        self.assertEqual(secretbox.unseal(token), "")


class TestPerUserKeys(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {"AUTOCODE_SLIM2_MASTER_KEY_FILE": str(Path(self._tmp.name) / "master.key")},
        )
        self._env.start()
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_keys_are_isolated(self) -> None:
        user_llm.save(self.db, "202600000001", {"api_key": KEY_A})
        user_llm.save(self.db, "202600000002", {"api_key": KEY_B})

        self.assertEqual(user_llm.config_for(self.db, "202600000001").api_key, KEY_A)
        self.assertEqual(user_llm.config_for(self.db, "202600000002").api_key, KEY_B)
        self.assertIsNone(user_llm.config_for(self.db, "202600000099"))

        state_a = user_llm.get(self.db, "202600000001")
        self.assertTrue(state_a["configured"])
        self.assertNotIn(KEY_A, str(state_a))
        self.assertEqual(state_a["api_key_masked"], "sk-a…1111")

    def test_clearing_one_keeps_the_other(self) -> None:
        user_llm.save(self.db, "202600000001", {"api_key": KEY_A})
        user_llm.save(self.db, "202600000002", {"api_key": KEY_B})
        user_llm.save(self.db, "202600000001", {"clear_key": True})
        self.assertIsNone(user_llm.config_for(self.db, "202600000001"))
        self.assertEqual(user_llm.config_for(self.db, "202600000002").api_key, KEY_B)

    def test_database_stores_ciphertext(self) -> None:
        user_llm.save(self.db, "202600000001", {"api_key": KEY_A})
        raw = self.db.query_one(
            "SELECT api_key_enc FROM user_llm_settings WHERE user=?", ("202600000001",)
        )["api_key_enc"]
        self.assertTrue(raw.startswith("v1:"))
        self.assertNotIn(KEY_A, raw)

    def test_reject_insecure_base_url(self) -> None:
        self.assertTrue(user_llm.base_url_error("http://api.deepseek.com/v1"))
        self.assertTrue(user_llm.base_url_error("https://127.0.0.1/v1"))
        self.assertTrue(user_llm.base_url_error("https://localhost:8443/v1"))
        self.assertEqual(user_llm.base_url_error("https://api.deepseek.com/v1"), "")

    def test_save_rejects_internal_base_url(self) -> None:
        result = user_llm.save(
            self.db, "202600000001", {"api_key": KEY_A, "base_url": "http://10.0.0.1/v1"}
        )
        self.assertEqual(result.get("code"), "INVALID_INPUT")
        self.assertIsNone(user_llm.config_for(self.db, "202600000001"))

    def test_calls_are_attributed_to_user(self) -> None:
        """额度按人算的前提：每次调用都要记下是谁花的。"""
        from autocode.slim2 import llm_stage
        from autocode.slim2.stages import StageSpec

        runner = llm_stage.StageRunner(
            self.db, budget=2, user="202600000001",
            llm_config=user_llm.config_for(self.db, "202600000001") or None,
        )
        spec = StageSpec(
            code="D1", template="t", version="1", temperature=0.0, max_tokens=8,
            timeout_s=5, retries=0, stage="d1",
        )
        runner._record(spec, attempt=1, ok=True)
        row = self.db.query_one("SELECT user, stage FROM llm_calls ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["user"], "202600000001")
        self.assertEqual(row["stage"], "d1")

    def test_worker_refuses_without_own_key(self) -> None:
        """别人有 Key 也不行：任务只认发起人自己的那一份。"""
        from types import SimpleNamespace

        from autocode.slim2 import worker
        from autocode.slim2.errors import StageFailure

        user_llm.save(self.db, "202600000002", {"api_key": KEY_B})
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(user, problem_id, kind, status, started_at)"
                " VALUES('202600000001', 1, 'dataset', 'running', '2026-11-22T00:00:00+00:00')"
            )
            job_id = int(cursor.lastrowid)

        with self.assertRaises(StageFailure) as ctx:
            worker._run(self.db, SimpleNamespace(job_id=job_id, problem_id=1, budget=2, command="dataset"))
        self.assertEqual(ctx.exception.stage, "config")


if __name__ == "__main__":
    unittest.main()
