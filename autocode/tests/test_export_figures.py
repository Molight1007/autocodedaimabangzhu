"""导出包带图测试：图片进 `figures/`，题面引用只改导出副本，数据库原文不动。

这是「接收准备」的最后一环：没有它，导出的题包到了别人机器上就没有插图。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autocode.store.db import Database, project_root, reset_default_db

#: 1x1 透明 PNG，足够验证"文件被打进 zip"
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)
STEM = "__test_export_figures__"


class ExportFigureCase(unittest.TestCase):
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

        self.asset_dir = project_root() / "data" / "assets" / STEM
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        (self.asset_dir / "fig_1.png").write_bytes(PNG_BYTES)

        self.statement = "1. 题目第一行\n2. 第二行\n3. 第三行\n"
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, statement_md, spec_json, status, source,"
                " created_at, updated_at)"
                " VALUES('带图题', ?, ?, 'prepared', 'CSP-PDF', 'now', 'now')",
                (self.statement, json.dumps({"checker_type": "exact"})),
            )
            self.pid = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO problem_figures(problem_id, label, page, bbox_json, path, kind,"
                " width, height, after_line, ordinal, created_at)"
                " VALUES(?, 'fig_1', 2, '[0,0,10,10]', ?, 'vector', 10, 10, 2, 1, 'now')",
                (self.pid, f"/assets/{STEM}/fig_1.png"),
            )

    def tearDown(self) -> None:
        reset_default_db()
        self.env.stop()
        self.db.close()
        self._tmp.cleanup()
        for leftover in self.asset_dir.glob("*"):
            leftover.unlink(missing_ok=True)
        self.asset_dir.rmdir()

    def test_figure_packed_and_referenced(self) -> None:
        from autocode.app.exporter import export_problem

        result = export_problem(self.pid, db=self.db)
        self.assertTrue(result.get("ok"), result)
        archive_path = Path(result["path"])
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn("figures/fig_1.png", names)
            self.assertEqual(archive.read("figures/fig_1.png"), PNG_BYTES)
            statement = archive.read("problem.md").decode("utf-8")
        self.assertIn("![fig_1](figures/fig_1.png)", statement)
        # 插在第 2 行之后：引用应出现在第二行之后、第三行之前
        self.assertLess(statement.index("第二行"), statement.index("![fig_1]"))
        self.assertLess(statement.index("![fig_1]"), statement.index("第三行"))

    def test_database_statement_untouched(self) -> None:
        from autocode.app.exporter import export_problem

        export_problem(self.pid, db=self.db)
        row = self.db.query_one("SELECT statement_md FROM problems WHERE id=?", (self.pid,))
        self.assertEqual(row["statement_md"], self.statement)

    def test_missing_file_degrades_silently(self) -> None:
        from autocode.app.exporter import export_problem

        (self.asset_dir / "fig_1.png").unlink()
        result = export_problem(self.pid, db=self.db)
        self.assertTrue(result.get("ok"), result)
        with zipfile.ZipFile(Path(result["path"])) as archive:
            self.assertNotIn("figures/fig_1.png", archive.namelist())
            statement = archive.read("problem.md").decode("utf-8")
        self.assertNotIn("![fig_1]", statement)


if __name__ == "__main__":
    unittest.main()
