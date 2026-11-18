"""提示分组与批次生命周期测试。

历史问题：每次重生成都插入一批新提示却不把旧批置 inactive，
题 1 累计了 6 批 × 4 级 = 24 条，前端一屏几十个大空盒子。
"""

from __future__ import annotations

import unittest

from autocode.web.api import _filter_latest_hint_batch


def _row(batch: str, level: int, approach: str = "") -> dict:
    return {"gen_batch_id": batch, "level": level, "approach": approach}


class TestLegacyHintBatchFilter(unittest.TestCase):
    def test_only_latest_batch_survives(self) -> None:
        rows = [
            _row("b1", 1), _row("b1", 2), _row("b1", 3),
            _row("b2", 1), _row("b2", 2), _row("b2", 3),
        ]
        kept = _filter_latest_hint_batch(rows)
        self.assertEqual([r["gen_batch_id"] for r in kept], ["b2", "b2", "b2"])

    def test_new_grouped_hints_are_not_trimmed(self) -> None:
        rows = [
            _row("b3:模拟", 1, "模拟"), _row("b3:暴力枚举", 1, "暴力枚举"),
            _row("b3:模拟", 2, "模拟"), _row("b3:暴力枚举", 2, "暴力枚举"),
        ]
        self.assertEqual(len(_filter_latest_hint_batch(rows)), 4)


class TestSaveHintsLifecycle(unittest.TestCase):
    def test_old_batch_deactivated_on_regenerate(self) -> None:
        from autocode.app.prep_store import PrepStore
        from autocode.store.db import Database
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(Path(tmp) / "h.db")
            db.init_schema()
            db.execute(
                "INSERT INTO problems(id,title,statement_md,status,created_at,updated_at)"
                " VALUES(1,'题','面','draft','now','now')"
            )
            store = PrepStore(db)
            store.save_hints(1, [{"level": 1, "content": "旧"}], "b1")
            store.save_hints(1, [
                {"level": 1, "content": "模拟L1", "approach": "模拟"},
                {"level": 1, "content": "暴力L1", "approach": "暴力枚举"},
            ], "b2")
            rows = db.query_all(
                "SELECT content,is_active,approach,gen_batch_id FROM hints ORDER BY id"
            )
            self.assertEqual(rows[0]["is_active"], 0, "旧批次必须置 inactive")
            self.assertEqual([r["approach"] for r in rows[1:]], ["模拟", "暴力枚举"])
            self.assertTrue(all(r["is_active"] == 1 for r in rows[1:]))
            self.assertEqual(len({r["gen_batch_id"] for r in rows[1:]}), 2, "每个解法独立批号")
            db.close()


if __name__ == "__main__":
    unittest.main()
