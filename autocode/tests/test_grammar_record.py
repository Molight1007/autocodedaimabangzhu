"""变长记录（record）：每行先给长度、再跟若干个数的输入形态。"""

from __future__ import annotations

import unittest

from autocode.engine.datagen import grammar as g


def _spec() -> list:
    return [
        {"kind": "scalar", "name": "n", "type": "int"},
        {"kind": "scalar", "name": "m", "type": "int"},
        {"kind": "repeat", "count": "n", "elem": {"kind": "record", "fields": [
            {"kind": "scalar", "name": "len", "type": "int"},
            {"kind": "repeat", "count": "len", "elem": {"type": "int", "name": "a"}},
        ]}},
    ]


class TestRecordGrammar(unittest.TestCase):
    def test_parse_records_with_leading_length(self) -> None:
        parsed = g.parse(_spec(), "4 3\n5 1 2 3 2 1\n1 1\n3 2 2 2\n2 3 2\n")
        self.assertEqual(parsed["n"], 4)
        self.assertEqual(len(parsed["arr2"]), 4)
        self.assertEqual(parsed["arr2"][0], {"len": 5, "a": [1, 2, 3, 2, 1]})
        self.assertEqual(parsed["arr2"][3], {"len": 2, "a": [3, 2]})
        self.assertEqual(g.validate(_spec(), parsed), [])

    def test_leftover_tokens_still_rejected(self) -> None:
        with self.assertRaises(g.GrammarError):
            g.parse(_spec(), "1 1\n2 1 2\n9\n")

    def test_validate_detects_missing_field(self) -> None:
        spec = _spec()
        bad = {"n": 1, "m": 1, "arr2": [{"len": 2}]}
        self.assertTrue(g.validate(spec, bad))

    def test_record_field_kind_must_be_scalar_or_repeat(self) -> None:
        spec = [{"kind": "repeat", "count": "1", "elem": {"kind": "record", "fields": [
            {"kind": "matrix", "name": "x", "rows": "1", "cols": "1"},
        ]}}]
        with self.assertRaises(g.GrammarError):
            g.parse(spec, "1 1\n")

    def test_record_needs_fields(self) -> None:
        spec = [{"kind": "repeat", "count": "1", "elem": {"kind": "record"}}]
        with self.assertRaises(g.GrammarError):
            g.parse(spec, "1\n")


if __name__ == "__main__":
    unittest.main()
