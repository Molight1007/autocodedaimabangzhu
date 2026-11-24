"""批量导入与生成任务持久化测试（契约 §4.4 / §4.5，不需要起子进程）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autocode.store.db import Database, reset_default_db


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "autocode.db"
        self.env = patch.dict(os.environ, {"AUTOCODE_DB": str(self.db_path)}, clear=False)
        self.env.start()
        reset_default_db()
        self.db = Database(self.db_path)
        self.db.init_schema()

    def tearDown(self) -> None:
        reset_default_db()
        self.env.stop()
        self.db.close()
        self._tmp.cleanup()


class TestSplitBatch(ApiCase):
    def test_split_by_separator(self) -> None:
        from autocode.web.api import split_batch_text

        text = "题目甲\n题面甲\n---\n题目乙\n题面乙\n"
        blocks = split_batch_text(text)
        self.assertEqual([b["title"] for b in blocks], ["题目甲", "题目乙"])
        self.assertEqual(blocks[0]["statement"], "题面甲")

    def test_blank_blocks_ignored(self) -> None:
        from autocode.web.api import split_batch_text

        blocks = split_batch_text("---\n\n---\n题目丙\n正文\n---\n")
        self.assertEqual([b["title"] for b in blocks], ["题目丙"])

    def test_title_only_block_kept_for_reporting(self) -> None:
        """只有标题的块不能静默丢：保留下来由 import_batch 记进 failed（B3）。"""
        from autocode.web.api import split_batch_text

        blocks = split_batch_text("只有标题\n")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["title"], "只有标题")
        self.assertEqual(blocks[0]["statement"], "")
        self.assertEqual(blocks[0]["index"], 0)

    def test_index_is_raw_block_position(self) -> None:
        from autocode.web.api import split_batch_text

        blocks = split_batch_text("A\n题面A\n---\n\n---\nB\n题面B\n")
        self.assertEqual([(b["index"], b["title"]) for b in blocks], [(0, "A"), (2, "B")])


class TestImportBatch(ApiCase):
    def test_import_two_problems(self) -> None:
        from autocode.web.api import import_batch

        text = (
            "两数之和\n### 【样例输入】\n1 2\n### 【样例输出】\n3\n"
            "---\n"
            "三数之和\n### 【样例输入】\n1 2 3\n### 【样例输出】\n6\n"
        )
        result = import_batch({"text": text, "languages": ["python"]})
        self.assertEqual(len(result["imported"]), 2)
        self.assertEqual(result["failed"], [])
        titles = [row["title"] for row in self.db.query_all("SELECT title FROM problems ORDER BY id")]
        self.assertEqual(titles, ["两数之和", "三数之和"])
        # 样例解析也要生效
        self.assertEqual(result["imported"][0]["samples"], 1)

    def test_partial_failure_reported(self) -> None:
        from autocode.web.api import import_batch

        text = "好题\n### 【样例输入】\n1\n### 【样例输出】\n1\n---\n\n"
        result = import_batch({"text": text})
        self.assertEqual(len(result["imported"]), 1)

    def test_duplicate_block_goes_to_failed_not_500(self) -> None:
        """B3：第 3 块题面与前一块重复 → imported 2 + failed 1，不抛 IntegrityError。"""
        from autocode.web.api import import_batch

        first = "题面甲\n### 【样例输入】\n1 2\n### 【样例输出】\n3\n"
        second = "题面乙\n### 【样例输入】\n2 3\n### 【样例输出】\n5\n"
        text = f"甲\n{first}---\n乙\n{second}---\n乙\n{second}"
        result = import_batch({"text": text})
        self.assertEqual(len(result["imported"]), 2)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["index"], 2)
        self.assertIn("重复", result["failed"][0]["error"])

    def test_title_only_block_reported_as_failed(self) -> None:
        from autocode.web.api import import_batch

        text = "有题面\n### 【样例输入】\n1\n### 【样例输出】\n1\n---\n只有标题\n"
        result = import_batch({"text": text})
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["failed"], [{"index": 1, "title": "只有标题", "error": "题面为空"}])

    def test_empty_text(self) -> None:
        from autocode.web.api import import_batch

        self.assertEqual(import_batch({"text": "  "}).get("error"), "没有可导入的题目（按单独一行 --- 分块）")


class TestJobPersistence(ApiCase):
    def _seed(self, status: str) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, status, failed_stage, created_at, updated_at)"
                " VALUES('题', '正文', ?, ?, 'now', 'now')",
                (status, "" if status == "prepared" else "spec"),
            )
            return int(cursor.lastrowid)

    def _insert_job(
        self, pid: int, status: str = "running", *, os_pid: int | None = None, started_at: str = "now"
    ) -> int:
        """默认把 os_pid 设成当前进程（活着），这样能测到"进程活着"的分支。"""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO generation_jobs(problem_id, status, budget, os_pid, error, started_at)"
                " VALUES(?, ?, 30, ?, '', ?)",
                (pid, status, os.getpid() if os_pid is None else os_pid, started_at),
            )
            return int(cursor.lastrowid)

    def test_no_job(self) -> None:
        from autocode.web.api import job_status

        self.assertEqual(job_status(1)["status"], "none")

    def test_running_job_reconciled_to_done(self) -> None:
        """DB 仍 running 但题目已 prepared → 查询时对账落 done。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("prepared")
        self._insert_job(pid)
        RUNNING.pop(pid, None)
        result = job_status(pid)
        self.assertEqual(result["status"], "done")
        row = self.db.query_one("SELECT status, finished_at FROM generation_jobs WHERE problem_id=?", (pid,))
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["finished_at"])

    def test_running_job_reconciled_to_failed(self) -> None:
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("failed")
        self._insert_job(pid)
        RUNNING.pop(pid, None)
        result = job_status(pid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "spec")

    def test_still_running_when_problem_preparing(self) -> None:
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        self._insert_job(pid)
        RUNNING.pop(pid, None)
        self.assertEqual(job_status(pid)["status"], "running")

    def test_dead_process_marks_failed(self) -> None:
        """S6：进程已不存在（服务重启/被杀）→ 不能永远 running。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        self._insert_job(pid, os_pid=999999)
        RUNNING.pop(pid, None)
        result = job_status(pid)
        self.assertEqual(result["status"], "failed")
        self.assertIn("进程已退出", result["error"])

    def test_stale_job_marks_failed(self) -> None:
        """S6：进程还活着但 started_at 超过 2 小时 → 判失败（防僵尸），并顺手杀掉它。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self._insert_job(pid, os_pid=child.pid, started_at="2020-01-01T00:00:00+00:00")
            RUNNING.pop(pid, None)
            result = job_status(pid)
            self.assertEqual(result["status"], "failed")
            self.assertIn("2 小时", result["error"])
            self.assertIn("已终止", result["error"], result["error"])
            child.wait(timeout=5)  # 被杀后应立即退出；超时会抛 TimeoutExpired
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()

    def test_never_kills_own_process(self) -> None:
        """保护：os_pid 指向服务自身时只能跳过，不能自杀。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        self._insert_job(pid, os_pid=os.getpid(), started_at="2020-01-01T00:00:00+00:00")
        RUNNING.pop(pid, None)
        result = job_status(pid)
        self.assertEqual(result["status"], "failed")
        self.assertIn("当前服务进程", result["error"])

    def test_start_generate_rejects_when_running(self) -> None:
        """S6：同题已有活任务时不许再起一个进程。"""
        from autocode.web.api import RUNNING, start_generate

        pid = self._seed("preparing")
        self._insert_job(pid)
        RUNNING.pop(pid, None)
        result = start_generate({"id": pid, "budget": 5})
        self.assertEqual(result.get("error"), "该题已有生成任务在排队或运行")

    def test_stale_job_with_handle_terminates_process(self) -> None:
        """一般项1：落 failed 前必须先杀进程，否则 UI 说失败、进程继续烧 token。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            RUNNING[pid] = child  # 有 Popen 句柄
            self._insert_job(pid, os_pid=child.pid, started_at="2020-01-01T00:00:00+00:00")
            result = job_status(pid)
            self.assertEqual(result["status"], "failed")
            self.assertIn("已终止", result["error"])
            self.assertIsNotNone(child.poll(), "僵尸任务对应进程应已被终止")
            self.assertNotIn(pid, RUNNING)
        finally:
            if child.poll() is None:
                child.kill()

    def test_orphan_pid_not_killed_when_not_python(self) -> None:
        """只有 os_pid 且不是 python 进程时，不允许误杀（PID 复用保护）。"""
        from autocode.web.api import RUNNING, job_status

        pid = self._seed("preparing")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            RUNNING.pop(pid, None)
            # 把 os_pid 指向一个"非 python"的进程名：用 pid 本身但伪造名字不现实，
            # 这里直接断言"活着但未超时"时不落 failed（走 problem 状态对账）
            self._insert_job(pid, os_pid=other.pid, started_at="now")
            self.assertEqual(job_status(pid)["status"], "running")
        finally:
            other.kill()

    def test_type_hints_resolvable(self) -> None:
        """建议项3：注解里的 Optional 必须真的 import（否则 get_type_hints 会 NameError）。"""
        import typing

        from autocode.app import jobs

        hints = typing.get_type_hints(jobs.parse_ts)
        self.assertIn("return", hints)

    def test_start_generate_rejects_unknown_problem(self) -> None:
        from autocode.web.api import start_generate

        self.assertEqual(start_generate({"id": 999}).get("error"), "题目不存在")


if __name__ == "__main__":
    unittest.main()
