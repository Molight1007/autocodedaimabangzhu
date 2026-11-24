"""排队执行：一类任务只有一个执行位，后来的按先来后到接上。

背景（2026-11-23）：原来第二道题只能收到「服务正忙」。改成排队后，
提交立刻返回位次，前一个落终态由调度器自动接下一个。
这里用假进程替掉 Popen，验证调度顺序与状态流转，不真跑生成流水线。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autocode.slim2 import config, jobs
from autocode.store.db import Database


class FakeProc:
    """够用的假子进程：只关心 poll 与 pid。"""

    _seq = 70000

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
        self.db = Database(self.root / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        self.spawned: list[FakeProc] = []

        def fake_popen(argv, **kwargs):
            proc = FakeProc(argv, **kwargs)
            self.spawned.append(proc)
            return proc

        self._popen_patch = patch.object(jobs, "_popen", side_effect=fake_popen)
        self._popen_patch.start()
        self._log_patch = patch.object(jobs, "_log_dir", return_value=self.root)
        self._log_patch.start()
        jobs.stop_scheduler()
        jobs.RUNNING.clear()

    def tearDown(self) -> None:
        self._popen_patch.stop()
        self._log_patch.stop()
        jobs.stop_scheduler()
        jobs.RUNNING.clear()
        self.db.close()
        self._tmp.cleanup()

    def start(self, problem_id: int, kind: str = "dataset", args=None) -> dict:
        return jobs.start(
            self.db,
            kind=kind,
            problem_id=problem_id,
            args=list(args or []),
            budget=8,
            user="tester",
        )

    def row(self, job_id: int):
        return self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def statuses(self) -> list[str]:
        return [str(r["status"]) for r in self.db.query_all("SELECT status FROM jobs ORDER BY id")]

    # ── 顺序 ────────────────────────────────────────────────────────────────
    def test_first_three_run_rest_queue_in_order(self) -> None:
        a = self.start(1)
        b = self.start(2)
        c = self.start(3)
        d = self.start(4)
        e = self.start(5)

        self.assertFalse(a["queued"])
        self.assertFalse(b["queued"])
        self.assertFalse(c["queued"])
        self.assertTrue(d["queued"])
        self.assertTrue(e["queued"])
        self.assertEqual(d["position"], 4)  # 前面 3 个在跑
        self.assertEqual(e["position"], 5)
        self.assertEqual(
            self.statuses(), ["running", "running", "running", "queued", "queued"]
        )
        self.assertEqual(len(self.spawned), 3, "最多只允许起 3 个 worker")

        got = jobs.by_id(self.db, d["job_id"])
        self.assertEqual(got["status"], "queued")
        self.assertEqual(got["ahead"], 3)
        self.assertEqual(got["elapsed_s"], 0, "排队时长不能算成执行时长")

    def test_finish_then_dispatch_starts_next(self) -> None:
        a = self.start(1)
        for pid in (2, 3, 4):
            self.start(pid)
        queued = self.db.query_one(
            "SELECT * FROM jobs WHERE problem_id=4 ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(queued["status"], "queued")

        jobs.finish(self.db, a["job_id"], "done", result={"ok": True})
        self.assertEqual(jobs.dispatch(self.db), 1)
        self.assertEqual(self.row(queued["id"])["status"], "running")
        self.assertEqual(len(self.spawned), 4)
        self.assertTrue(self.row(queued["id"])["started_at"])

    def test_duplicate_problem_is_rejected(self) -> None:
        self.start(1)
        again = self.start(1)
        self.assertEqual(again.get("code"), "JOB_RUNNING")

        # 排队中的题也不允许再提交一条
        self.start(2)
        self.start(3)
        queued = self.start(4)
        self.assertTrue(queued["queued"])
        duplicate = self.start(4)
        self.assertEqual(duplicate.get("code"), "JOB_RUNNING")

    def test_dataset_and_adversarial_share_one_queue(self) -> None:
        """测试数据和对抗数据一起排：没有独立槽位，谁先来谁先跑。"""
        for pid in (1, 2, 3):
            self.assertFalse(self.start(pid, "dataset")["queued"])
        adv = self.start(4, "adversarial", args=["--submission-id", "44"])
        self.assertTrue(adv["queued"], "3 个执行位已满，对抗任务也要排队")
        self.assertEqual(adv["position"], 4)

        # 放掉一个测试数据任务，队头的对抗任务接上（不是"等所有测试数据跑完"）
        first = self.db.query_one("SELECT id FROM jobs WHERE kind='dataset' ORDER BY id LIMIT 1")
        jobs.finish(self.db, int(first["id"]), "done", result={})
        self.assertEqual(jobs.dispatch(self.db), 1)
        self.assertEqual(self.row(adv["job_id"])["status"], "running")

    def test_adversarial_args_survive_queue(self) -> None:
        for pid in (1, 2, 3):
            self.start(pid, "dataset")
        queued = self.start(4, "adversarial", args=["--submission-id", "22"])
        self.assertTrue(queued["queued"])
        stored = json.loads(self.row(queued["job_id"])["args_json"])
        self.assertEqual(stored, ["--submission-id", "22"])

        first = self.db.query_one("SELECT id FROM jobs WHERE kind='dataset' ORDER BY id LIMIT 1")
        jobs.finish(self.db, int(first["id"]), "done", result={})
        jobs.dispatch(self.db)
        self.assertIn("--submission-id", self.spawned[-1].argv)
        self.assertIn("22", self.spawned[-1].argv)

    # ── 取消 / 重启 ─────────────────────────────────────────────────────────
    def test_cancel_queued_does_not_touch_running_job(self) -> None:
        running = [self.start(pid) for pid in (1, 2, 3)]
        b = self.start(4)
        c = self.start(5)

        result = jobs.cancel_by_id(self.db, b["job_id"])
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.row(b["job_id"])["status"], "failed")
        self.assertEqual(self.row(b["job_id"])["failed_stage"], "cancelled")
        self.assertEqual(self.row(running[0]["job_id"])["status"], "running")
        self.assertEqual(len(self.spawned), 3, "取消排队任务不能杀正在跑的进程")

        # 后面的位次自动前移
        self.assertEqual(jobs.by_id(self.db, c["job_id"])["ahead"], 3)

    def test_restart_keeps_queue_and_live_process_blocks_slot(self) -> None:
        """模拟重启：DB 留着 1 个 running 且进程还活着 → 只剩 2 个空位；排队任务不丢。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(problem_id,kind,status,stage,budget,os_pid,error,failed_stage,"
                " result_json,args_json,queued_at,started_at)"
                " VALUES(1,'dataset','running','',8,?,'','',NULL,'[]',?,?)",
                (os.getpid(), now, now),
            )
            running_id = int(cursor.lastrowid)
        jobs.RUNNING.clear()  # 重启后内存里的句柄没了，只能靠 pid 判活

        self.assertFalse(self.start(2)["queued"])
        self.assertFalse(self.start(3)["queued"])
        queued = self.start(4)
        self.assertTrue(queued["queued"])
        self.assertEqual(jobs.dispatch(self.db), 0)
        self.assertEqual(self.row(queued["job_id"])["status"], "queued")

        jobs.finish(self.db, running_id, "failed", "服务重启，任务被中断", "interrupted")
        self.assertEqual(jobs.dispatch(self.db), 1)
        self.assertEqual(self.row(queued["job_id"])["status"], "running")

    def test_long_wait_does_not_timeout(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(problem_id,kind,status,stage,budget,os_pid,error,failed_stage,"
                " result_json,args_json,queued_at,started_at)"
                " VALUES(1,'dataset','queued','',8,0,'','',NULL,'[]',?,'')",
                (old,),
            )
            job_id = int(cursor.lastrowid)
        got = jobs.by_id(self.db, job_id)
        self.assertEqual(got["status"], "queued", "排队时间再长也不能被判超时")

    def test_start_reports_spawn_failure(self) -> None:
        with patch.object(jobs, "_popen", side_effect=OSError("no python")):
            result = self.start(1)
        self.assertFalse(result.get("ok"))
        self.assertIn("无法启动", result.get("error", ""))
        self.assertEqual(self.statuses(), ["failed"])

    # ── 接口返回 ────────────────────────────────────────────────────────────
    def test_api_start_dataset_returns_position(self) -> None:
        """前端要拿 position 才能显示「排第几」；不再用「服务正忙」挡人。"""
        from autocode.slim2 import api

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            for index in (1, 2, 3, 4):
                conn.execute(
                    "INSERT INTO problems(bank_id,title,statement_md,statement_hash,status,"
                    " judge_type,samples_json,created_at,updated_at)"
                    " VALUES(1,?,?,?,'draft','token','[]',?,?)",
                    (f"题 {index}", f"题面 {index}", f"hash-{index}", now, now),
                )
        results = [api.start_dataset(self.db, {"id": pid}, user="tester") for pid in (1, 2, 3, 4)]
        self.assertTrue(all(item.get("ok") for item in results), results)
        self.assertFalse(any(item["queued"] for item in results[:3]))
        self.assertTrue(results[3]["queued"])
        self.assertEqual(results[3]["position"], 4)
        self.assertEqual(self.row(results[3]["job_id"])["status"], "queued")

    def test_concurrent_double_submit_creates_one_job(self) -> None:
        """两个页签同时点同一道题：只允许一条排队记录，不能双份烧额度。"""
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
        accepted = [item for item in results if item.get("ok")]
        self.assertEqual(len(accepted), 1, results)
        total = self.db.query_scalar("SELECT COUNT(*) FROM jobs WHERE problem_id=1", (), 0)
        self.assertEqual(total, 1)

    # ── 调度线程 ────────────────────────────────────────────────────────────
    def test_scheduler_starts_next_after_finish(self) -> None:
        running = [self.start(pid) for pid in (1, 2, 3)]
        queued = self.start(4)
        jobs.ensure_scheduler(self.db, interval=0.05)
        try:
            jobs.finish(self.db, running[0]["job_id"], "done", result={})
            deadline = time.time() + 3
            while time.time() < deadline and self.row(queued["job_id"])["status"] != "running":
                time.sleep(0.05)
            self.assertEqual(self.row(queued["job_id"])["status"], "running")
        finally:
            jobs.stop_scheduler()


if __name__ == "__main__":
    unittest.main()
