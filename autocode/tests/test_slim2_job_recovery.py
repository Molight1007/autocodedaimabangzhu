"""服务重启后遗留任务的对账 + 子进程回收。

背景（2026-11-22）：部署补丁时 `systemctl restart` 连坐杀掉正在跑的 worker，
job 停在「数据复核」。数据库要等前端来查才更新，更新的理由又是「进程异常」，
界面上就是「测试数据失败（进程异常）」，用户会以为程序崩了。

两条约束：
1. 启动时只把「pid 已经不在了」的 running 任务标为中断 —— 手工拉起的进程不能误伤；
2. worker 退出后必须有人 poll，否则每跑一题留一个僵尸进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autocode.slim2 import config, jobs
from autocode.store.db import Database


class TestJobRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(id,title,statement_md,statement_hash,status,created_at,updated_at)"
                " VALUES(1,'t','s','h','generating',?,?)",
                (now, now),
            )

    def tearDown(self) -> None:
        jobs.RUNNING.clear()
        self.db.close()
        self._tmp.cleanup()

    def _job(self, pid: int, status: str = "running") -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(problem_id,kind,status,stage,budget,os_pid,error,failed_stage,"
                "result_json,started_at) VALUES(1,'dataset',?,'数据生成',8,?,'','',NULL,?)",
                (status, pid, now),
            )
            return int(cursor.lastrowid)

    def test_dead_process_is_marked_interrupted(self) -> None:
        job_id = self._job(0)
        self.assertEqual(jobs.mark_orphans(self.db), 1)
        got = jobs.by_id(self.db, job_id)
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["failed_stage"], "interrupted")
        self.assertIn("重启", got["error"])
        # 题目必须从「生成中」放出来，否则界面永远转圈
        problem = self.db.query_all("SELECT status FROM problems WHERE id=1")[0]
        self.assertEqual(problem["status"], "failed")

    def test_live_process_is_left_alone(self) -> None:
        job_id = self._job(os.getpid())
        self.assertEqual(jobs.mark_orphans(self.db), 0)
        self.assertEqual(jobs.by_id(self.db, job_id)["status"], "running")

    def test_finished_worker_is_reaped(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
        time.sleep(0.4)  # 不用 wait()：要复现「退出但没人 poll」的僵尸态
        jobs.RUNNING[("dataset", 1)] = proc
        job_id = self._job(proc.pid, status="done")
        jobs.by_id(self.db, job_id)
        self.assertNotIn(("dataset", 1), jobs.RUNNING)
        self.assertIsNotNone(proc.returncode)


if __name__ == "__main__":
    unittest.main()
