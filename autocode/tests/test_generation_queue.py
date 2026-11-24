"""本地工作台的生成队列：一次只跑一题，后面的按先来后到接上。

和 slim2 的排队规则一致，区别只在表名（generation_jobs）与进程参数。
这里用假进程替掉 Popen，验证顺序、位次、重启对账，不真跑生成流水线。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autocode.app import jobs
from autocode.store.db import Database

_SCHEMA = Path(__file__).resolve().parents[1] / "store" / "schema.sql"


class FakeProc:
    _seq = 80000

    def __init__(self, argv, **_kwargs) -> None:
        FakeProc._seq += 1
        self.pid = FakeProc._seq
        self.argv = list(argv)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.logs = self.root / "logs"
        self.db = Database(self.root / "autocode.db")
        self.db.connect().executescript(_SCHEMA.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            for index in range(1, 4):
                conn.execute(
                    "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
                    " VALUES(?,?,'draft',?,?)",
                    (f"题 {index}", f"题面 {index}", now, now),
                )
        self.spawned: list[FakeProc] = []

        def fake_popen(argv, **kwargs):
            proc = FakeProc(argv, **kwargs)
            self.spawned.append(proc)
            return proc

        self._popen_patch = patch.object(jobs, "_popen", side_effect=fake_popen)
        self._popen_patch.start()
        jobs.stop_scheduler()
        jobs.RUNNING.clear()

    def tearDown(self) -> None:
        self._popen_patch.stop()
        jobs.stop_scheduler()
        jobs.RUNNING.clear()
        self.db.close()
        self._tmp.cleanup()

    def start(self, problem_id: int) -> dict:
        return jobs.start_generate(
            self.db, {"id": problem_id, "budget": 5}, root=self.root, log_dir=self.logs
        )

    def dispatch(self) -> bool:
        return jobs.dispatch(self.db, root=self.root, log_dir=self.logs)

    def row(self, job_id: int):
        return self.db.query_one("SELECT * FROM generation_jobs WHERE id=?", (job_id,))

    def statuses(self) -> list[str]:
        return [
            str(r["status"])
            for r in self.db.query_all("SELECT status FROM generation_jobs ORDER BY id")
        ]

    # ── 顺序 ────────────────────────────────────────────────────────────────
    def test_first_runs_rest_queue(self) -> None:
        a = self.start(1)
        b = self.start(2)
        c = self.start(3)

        self.assertFalse(a["queued"])
        self.assertTrue(b["queued"])
        self.assertTrue(c["queued"])
        self.assertEqual(b["position"], 2)
        self.assertEqual(c["position"], 3)
        self.assertEqual(self.statuses(), ["running", "queued", "queued"])
        self.assertEqual(len(self.spawned), 1, "只允许起一个生成进程")

        got = jobs.job_status(self.db, 2)
        self.assertEqual(got["status"], "queued")
        self.assertEqual(got["ahead"], 1)
        self.assertEqual(got["position"], 2)

    def test_finish_then_dispatch_starts_next(self) -> None:
        a = self.start(1)
        b = self.start(2)
        jobs.close_job(self.db, a["job_id"], "done", "")

        self.assertEqual(self.row(b["job_id"])["status"], "queued")
        self.assertTrue(self.dispatch())
        self.assertEqual(self.row(b["job_id"])["status"], "running")
        self.assertEqual(len(self.spawned), 2)
        self.assertTrue(self.row(b["job_id"])["started_at"])

    def test_duplicate_problem_rejected(self) -> None:
        self.start(1)
        again = self.start(1)
        self.assertIn("排队或运行", again.get("error", ""))

        self.start(2)
        self.assertIn("排队或运行", self.start(2).get("error", ""))

    def test_unknown_problem(self) -> None:
        self.assertEqual(self.start(999).get("error"), "题目不存在")

    # ── 重启 / 超时 ─────────────────────────────────────────────────────────
    def test_live_process_blocks_slot_after_restart(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO generation_jobs(problem_id,status,budget,os_pid,error,queued_at,"
                " started_at) VALUES(1,'running',5,?,'','',?)",
                (os.getpid(), now),
            )
            running_id = int(cursor.lastrowid)
        jobs.RUNNING.clear()  # 重启后内存句柄没了，只能靠 pid 判活

        queued = self.start(2)
        self.assertTrue(queued["queued"])
        self.assertFalse(self.dispatch())
        self.assertEqual(self.row(queued["job_id"])["status"], "queued")

        jobs.close_job(self.db, running_id, "failed", "服务重启，任务被中断")
        self.assertTrue(self.dispatch())
        self.assertEqual(self.row(queued["job_id"])["status"], "running")

    def test_long_wait_does_not_timeout(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO generation_jobs(problem_id,status,budget,os_pid,error,queued_at,"
                " started_at) VALUES(1,'queued',5,0,'',?,'')",
                (old,),
            )
            job_id = int(cursor.lastrowid)
        got = jobs.job_status(self.db, 1)
        self.assertEqual(got["status"], "queued", "排队时间再长也不能被判超时")
        self.assertEqual(got["job_id"], job_id)

    def test_cli_guard_sees_queued_job(self) -> None:
        self.start(1)
        self.start(2)
        message = jobs.running_job_error(self.db, 2)
        self.assertIsNotNone(message)
        self.assertIn("排队", message)

    def test_spawn_failure_is_reported(self) -> None:
        with patch.object(jobs, "_popen", side_effect=OSError("no python")):
            result = self.start(1)
        self.assertIn("无法启动", result.get("error", ""))
        self.assertEqual(self.statuses(), ["failed"])

    def test_concurrent_double_submit_creates_one_job(self) -> None:
        """两个页签同时点同一道题：只允许一条排队记录。"""
        import threading

        results = []
        lock = threading.Lock()

        def submit() -> None:
            got = self.start(1)
            with lock:
                results.append(got)

        threads = [threading.Thread(target=submit) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        accepted = [item for item in results if item.get("started")]
        self.assertEqual(len(accepted), 1, results)
        total = self.db.query_scalar(
            "SELECT COUNT(*) FROM generation_jobs WHERE problem_id=1", (), 0
        )
        self.assertEqual(total, 1)

    # ── 调度线程 ────────────────────────────────────────────────────────────
    def test_scheduler_starts_next_after_finish(self) -> None:
        a = self.start(1)
        b = self.start(2)
        jobs.ensure_scheduler(self.db, root=self.root, log_dir=self.logs, interval=0.05)
        try:
            jobs.close_job(self.db, a["job_id"], "done", "")
            deadline = time.time() + 3
            while time.time() < deadline and self.row(b["job_id"])["status"] != "running":
                time.sleep(0.05)
            self.assertEqual(self.row(b["job_id"])["status"], "running")
        finally:
            jobs.stop_scheduler()


if __name__ == "__main__":
    unittest.main()
