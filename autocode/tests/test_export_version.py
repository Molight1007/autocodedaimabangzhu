"""导出版本测试（契约 §4.3）：连续导出 v1/v2、历史两条、zip 内容可读。"""

from __future__ import annotations

import gc
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autocode.store.db import Database, reset_default_db


class ExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "autocode.db"
        self.export_dir = Path(self._tmp.name) / "exports"
        self.env = patch.dict(
            os.environ,
            {"AUTOCODE_DB": str(self.db_path), "AUTOCODE_EXPORT_DIR": str(self.export_dir)},
            clear=False,
        )
        self.env.start()
        reset_default_db()
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.pid = self._seed_problem()
        # 导出目录用 AUTOCODE_EXPORT_DIR 隔离到临时目录，绝不污染仓库 exports/

    def tearDown(self) -> None:
        reset_default_db()
        self.env.stop()
        self.db.close()
        self._tmp.cleanup()

    def _seed_problem(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, spec_json, status, source,"
                " created_at, updated_at)"
                " VALUES('仓库/规划:测试', '题面正文', ?, 'prepared', 'CSP', 'now', 'now')",
                (
                    json.dumps(
                        {
                            "checker_type": "exact",
                            "samples": [{"input": "1\n", "output": "1\n"}],
                            "constraints_exec": ["1 <= n <= 1000"],
                        }
                    ),
                ),
            )
            pid = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " expected_output, source_of_truth, trust_level, size_class, source)"
                " VALUES(?, 'sample', 1, '1 2\n', 'h1', '3\n', 'sample', 'oracle', 'sample', 'generated')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO reference_solutions(problem_id, lang, role, code, verified,"
                " samples_expected, verification_level, created_at)"
                " VALUES(?, 'python', 'reference', 'print(1)', 1, 1, 'cross_verified', 'now')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO hints(problem_id, level, content, gen_batch_id, is_active)"
                " VALUES(?, 1, '提示一', 'b1', 1)",
                (pid,),
            )
        return pid


class TestVersions(ExportCase):
    def test_two_exports_v1_then_v2(self) -> None:
        from autocode.web import api

        first = api.export_problem(self.pid)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["version"], 1)
        self.assertIn("-v1.zip", first["filename"])

        second = api.export_problem(self.pid)
        self.assertEqual(second["version"], 2)
        self.assertIn("-v2.zip", second["filename"])
        self.assertNotEqual(first["filename"], second["filename"])

        history = api.list_exports(self.pid)
        self.assertEqual(len(history), 2)
        self.assertEqual([row["version"] for row in history], [2, 1])  # 最近的在前

    def test_history_fields(self) -> None:
        from autocode.web import api

        result = api.export_problem(self.pid)
        row = api.list_exports(self.pid)[0]
        self.assertEqual(row["filename"], result["filename"])
        self.assertEqual(row["bytes"], result["bytes"])
        self.assertEqual(row["cases"], 1)
        self.assertTrue(row["created_at"])

    def test_filename_sanitises_title(self) -> None:
        """标题里的 / 与 : 不能出现在文件名里（Windows 非法字符）。"""
        from autocode.web import api

        result = api.export_problem(self.pid)
        self.assertNotIn("/", result["filename"])
        self.assertNotIn(":", result["filename"])

    def test_zip_content(self) -> None:
        from autocode.web import api

        result = api.export_problem(self.pid)
        data = Path(result["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            self.assertIn("problem.md", names)
            self.assertIn("cases.json", names)
            self.assertIn("meta.json", names)
            meta = json.loads(archive.read("meta.json").decode("utf-8"))
            self.assertEqual(meta["version"], 1)
            cases = json.loads(archive.read("cases.json").decode("utf-8"))
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["input"], "1 2\n")

    def test_cases_payload_keeps_trust_and_source(self) -> None:
        """S3：导出包必须能区分 oracle 与 force 入库的未校验数据。"""
        from autocode.web import api

        result = api.export_problem(self.pid)
        with zipfile.ZipFile(result["path"]) as archive:
            cases = json.loads(archive.read("cases.json").decode("utf-8"))
        self.assertEqual(len(cases), 1)
        row = cases[0]
        self.assertEqual(row["trust_level"], "oracle")
        self.assertEqual(row["source"], "generated")
        self.assertTrue(row["id"].startswith("case"), row["id"])

    def test_meta_constraints_from_exec_key(self) -> None:
        """S3：真实 spec 用的是 constraints_exec，旧实现读 constraints 恒为空。"""
        from autocode.web import api

        result = api.export_problem(self.pid)
        with zipfile.ZipFile(result["path"]) as archive:
            meta = json.loads(archive.read("meta.json").decode("utf-8"))
        self.assertEqual(meta["constraints"], ["1 <= n <= 1000"])

    def test_case_ids_unique_across_layers(self) -> None:
        """S4：跨层不能用 ordinal 重号，必须用 DB 主键。"""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " expected_output, trust_level, source)"
                " VALUES(?, 'user', 1, '9 9\n', 'h-user', '18\n', 'reference_only', 'user')",
                (self.pid,),
            )
        from autocode.web import api

        result = api.export_problem(self.pid)
        with zipfile.ZipFile(result["path"]) as archive:
            cases = json.loads(archive.read("cases.json").decode("utf-8"))
        ids = [row["id"] for row in cases]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_concurrent_exports_unique_versions(self) -> None:
        """B2：8 线程并发导出 → 版本 1..8 唯一、历史 8 条、无异常。"""
        import threading

        from autocode.web import api

        results, errors = [], []
        lock = threading.Lock()

        def worker() -> None:
            try:
                value = api.export_problem(self.pid)
            except Exception as exc:  # noqa: BLE001 - 任何异常都算失败
                with lock:
                    errors.append(repr(exc))
                return
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        gc.collect()  # 让线程里的 per-thread 连接尽快释放，避免临时库被占用

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        versions = sorted(item["version"] for item in results)
        self.assertEqual(versions, list(range(1, 9)))
        filenames = {item["filename"] for item in results}
        self.assertEqual(len(filenames), 8)
        rows = self.db.query_all(
            "SELECT version FROM export_history WHERE problem_id=? ORDER BY version", (self.pid,)
        )
        self.assertEqual([row["version"] for row in rows], list(range(1, 9)))
        for item in results:
            self.assertTrue(Path(item["path"]).exists())

    def test_build_failure_rolls_back_history(self) -> None:
        """建议项4：打包失败不能留下"有历史没文件"，也不能留 .tmp 残渣。"""
        from unittest.mock import patch as _patch

        from autocode.app import exporter
        from autocode.web import api

        with _patch.object(exporter, "_build_zip", side_effect=OSError("disk full")):
            result = api.export_problem(self.pid)
        self.assertIn("写导出文件失败", result.get("error", ""))
        self.assertEqual(
            int(self.db.query_scalar("SELECT COUNT(*) FROM export_history WHERE problem_id=?", (self.pid,))),
            0,
        )
        leftovers = list(self.export_dir.glob("*")) if self.export_dir.exists() else []
        self.assertEqual(leftovers, [])
        # 补偿后版本号可复用：接着导出应拿到 v1
        ok = api.export_problem(self.pid)
        self.assertEqual(ok["version"], 1)

    def test_missing_problem(self) -> None:
        from autocode.web import api

        self.assertEqual(api.export_problem(9999).get("error"), "题目不存在")
        self.assertEqual(api.list_exports(9999), [])

    def test_history_limit_ten(self) -> None:
        from autocode.web import api

        for _ in range(12):
            api.export_problem(self.pid)
        self.assertEqual(len(api.list_exports(self.pid)), 10)
        self.assertEqual(api.list_exports(self.pid)[0]["version"], 12)


if __name__ == "__main__":
    unittest.main()
