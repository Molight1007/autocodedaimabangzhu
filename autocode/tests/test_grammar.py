"""grammar 测试：scalar / repeat / matrix / allow_empty / constraints。"""

from __future__ import annotations

import unittest

from autocode.engine.datagen import grammar as G

MATRIX_GRAMMAR = [
    {"kind": "scalar", "name": "n", "type": "int"},
    {"kind": "scalar", "name": "m", "type": "int"},
    {"kind": "matrix", "name": "a", "rows": "n", "cols": "m", "elem": {"type": "int"}},
]

REPEAT_GRAMMAR = [
    {"kind": "scalar", "name": "n", "type": "int"},
    {"kind": "repeat", "count": "n", "sep": "space", "elem": {"type": "int", "name": "a"}},
]


class TestParse(unittest.TestCase):
    def test_matrix(self) -> None:
        parsed = G.parse(MATRIX_GRAMMAR, "4 2\n0 0\n-1 -1\n1 2\n0 -1\n")
        self.assertEqual(parsed["n"], 4)
        self.assertEqual(parsed["a"][1], [-1, -1])

    def test_repeat_space_multiline(self) -> None:
        parsed = G.parse(REPEAT_GRAMMAR, "5\n1 2\n3 4 5\n")
        self.assertEqual(parsed["a"], [1, 2, 3, 4, 5])

    def test_trailing_tokens_rejected(self) -> None:
        with self.assertRaises(G.GrammarError):
            G.parse(REPEAT_GRAMMAR, "3\n1 2 3 4\n")

    def test_short_input_rejected(self) -> None:
        with self.assertRaises(G.GrammarError):
            G.parse(MATRIX_GRAMMAR, "2 2\n1 2\n")

    def test_non_integer_rejected(self) -> None:
        with self.assertRaises(G.GrammarError):
            G.parse(REPEAT_GRAMMAR, "2\n1 x\n")

    def test_repeat_newline_requires_own_line(self) -> None:
        grammar = [
            {"kind": "scalar", "name": "n", "type": "int"},
            {"kind": "repeat", "count": "n", "sep": "newline", "elem": {"type": "int", "name": "a"}},
        ]
        self.assertEqual(G.parse(grammar, "2\n7\n9\n")["a"], [7, 9])
        with self.assertRaises(G.GrammarError):
            G.parse(grammar, "2\n7 9\n")

    def test_allow_empty_zero_length(self) -> None:
        grammar = [
            {"kind": "scalar", "name": "n", "type": "int"},
            {"kind": "repeat", "count": "n", "allow_empty": True, "elem": {"type": "int", "name": "a"}},
        ]
        parsed = G.parse(grammar, "0\n")
        self.assertEqual(parsed["a"], [])
        self.assertEqual(G.validate(grammar, parsed), [])

    def test_empty_without_allow_empty_rejected(self) -> None:
        with self.assertRaises(G.GrammarError):
            G.parse(REPEAT_GRAMMAR, "0\n")

    def test_empty_array_kind(self) -> None:
        grammar = [{"kind": "empty_array", "name": "a"}]
        self.assertEqual(G.parse(grammar, "0\n"), {"a": []})
        with self.assertRaises(G.GrammarError):
            G.parse(grammar, "3\n")

    def test_matrix_zero_needs_allow_empty(self) -> None:
        grammar = [
            {"kind": "scalar", "name": "n", "type": "int"},
            {"kind": "matrix", "name": "a", "rows": "n", "cols": 2, "elem": {"type": "int"}},
        ]
        with self.assertRaises(G.GrammarError):
            G.parse(grammar, "0\n")


class TestValidate(unittest.TestCase):
    def test_clean_value(self) -> None:
        parsed = G.parse(REPEAT_GRAMMAR, "3\n1 2 3\n")
        self.assertEqual(G.validate(REPEAT_GRAMMAR, parsed), [])

    def test_length_mismatch_flagged(self) -> None:
        violations = G.validate(REPEAT_GRAMMAR, {"n": 5, "a": [1, 2]})
        self.assertTrue(any("长度" in item for item in violations))

    def test_missing_key_flagged(self) -> None:
        self.assertTrue(G.validate(REPEAT_GRAMMAR, {"n": 3}))

    def test_matrix_wrong_cols(self) -> None:
        violations = G.validate(MATRIX_GRAMMAR, {"n": 2, "m": 3, "a": [[1, 2, 3], [1, 2]]})
        self.assertTrue(any("列数" in item for item in violations))


class TestSpecCheck(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(G.check_spec(MATRIX_GRAMMAR), [])

    def test_unknown_kind(self) -> None:
        self.assertTrue(G.check_spec([{"kind": "graph", "name": "g"}]))

    def test_unknown_count_ref(self) -> None:
        grammar = [{"kind": "repeat", "count": "k", "elem": {"type": "int"}}]
        self.assertTrue(any("未知 scalar" in item for item in G.check_spec(grammar)))

    def test_dict_wrapper_accepted(self) -> None:
        self.assertEqual(G.check_spec({"input_grammar": REPEAT_GRAMMAR}), [])

    def test_describe(self) -> None:
        self.assertIn("n:int", G.describe(MATRIX_GRAMMAR))


class TestConstraints(unittest.TestCase):
    def test_pass_and_fail(self) -> None:
        checks = G.evaluate_constraints(["1 <= n <= 1000", "0 < m <= 10"], {"n": 4, "m": 2})
        self.assertTrue(all(item.ok for item in checks))
        checks = G.evaluate_constraints(["1 <= n <= 1000"], {"n": 0})
        self.assertFalse(checks[0].ok)

    def test_array_name_means_length(self) -> None:
        checks = G.evaluate_constraints(["n == 3"], {"n": 3, "a": [1, 2, 3]})
        self.assertTrue(checks[0].ok)

    def test_unsupported_syntax_flagged(self) -> None:
        checks = G.evaluate_constraints(["__import__('os')"], {"n": 1})
        self.assertFalse(checks[0].ok)
        self.assertIn("不支持的语法", checks[0].detail)


if __name__ == "__main__":
    unittest.main()
