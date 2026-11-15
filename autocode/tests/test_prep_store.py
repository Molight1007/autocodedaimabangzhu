"""用例落库的幂等性：重跑不能把测试数据越堆越多。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autocode.app.prep_store import PrepStore
from autocode.store.db import Database


class TestSaveCasesIdempotent(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()
        self.store = PrepStore(self.db)
        self.db.execute(
            "INSERT INTO problems(id, title, statement_md) VALUES(1, 't', 'stmt')"
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    @staticmethod
    def _case(text: str) -> dict:
        return {
            "layer": "exhaustive",
            "input": text,
            "expected": text,
            "source": "oracle",
            "trust": "oracle",
        }

    def test_rerun_replaces_layer_instead_of_accumulating(self) -> None:
        self.assertEqual(self.store.save_cases(1, [self._case("1\n")]), {"exhaustive": 1})
        self.assertEqual(
            self.store.save_cases(1, [self._case("2\n"), self._case("3\n")]),
            {"exhaustive": 2},
        )
        self.assertEqual(
            self.db.query_scalar("SELECT COUNT(*) FROM test_cases WHERE problem_id = 1"),
            2,
        )

    def test_other_layers_untouched(self) -> None:
        self.store.save_cases(1, [self._case("1\n")])
        self.store.save_cases(1, [{"layer": "stress", "input": "9\n", "expected": "9\n"}])
        self.store.save_cases(1, [self._case("2\n")])
        rows = self.db.query_all(
            "SELECT layer, COUNT(*) AS n FROM test_cases WHERE problem_id = 1 GROUP BY layer"
        )
        counts = {row["layer"]: row["n"] for row in rows}
        self.assertEqual(counts, {"exhaustive": 1, "stress": 1})


if __name__ == "__main__":
    unittest.main()
