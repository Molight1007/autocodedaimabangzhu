"""任务的「已用时」必须由服务端算。

背景（2026-11-21）：界面用 `Date.now() - started_at` 自己减，某台客户端比
服务器慢 21 秒，于是真实跑了 28 秒时界面显示「已 7 秒」。这个数字一旦偏，
用户就更分不清「还在跑」和「卡死了」——所以改由 `jobs.elapsed_seconds`
按服务端时钟算，前端只负责显示。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autocode.slim2 import config, jobs
from autocode.store.db import Database


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class TestElapsed(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 自建库，不走 slim2.db.get_db：后者 apply_env() 会改写全局 AUTOCODE_DB
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _job(self, started: str, finished: str | None = None, *, status: str = "running") -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(problem_id,kind,status,stage,budget,os_pid,error,failed_stage,"
                "result_json,started_at,finished_at) VALUES(1,'dataset',?,'数据生成',8,?,'','',NULL,?,?)",
                (status, os.getpid(), started, finished),
            )
            return int(cursor.lastrowid)

    def test_running_job_elapsed_comes_from_server(self) -> None:
        job_id = self._job(_ago(90))
        got = jobs.by_id(self.db, job_id)
        self.assertEqual(got["status"], "running")
        self.assertGreaterEqual(got["elapsed_s"], 89)
        self.assertLess(got["elapsed_s"], 120)

    def test_finished_job_elapsed_stops_at_finish(self) -> None:
        started = _ago(300)
        finished = (datetime.fromisoformat(started) + timedelta(seconds=61)).isoformat()
        job_id = self._job(started, finished, status="failed")
        got = jobs.by_id(self.db, job_id)
        self.assertEqual(got["elapsed_s"], 61)

    def test_missing_job_reports_zero(self) -> None:
        got = jobs.status(self.db, 404)
        self.assertEqual(got["status"], "none")
        self.assertEqual(got["elapsed_s"], 0)


if __name__ == "__main__":
    unittest.main()
