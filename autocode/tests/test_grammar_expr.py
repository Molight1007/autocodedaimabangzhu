"""grammar 的受限算术表达式：树/图题的 `n-1`、`(m+1)//2` 这类计数。"""

from __future__ import annotations

import unittest

from autocode.engine.datagen import grammar as g


class TestCountExpressions(unittest.TestCase):
    def _spec(self, count: str) -> list:
        return [
            {"kind": "scalar", "name": "n", "type": "int"},
            {"kind": "scalar", "name": "m", "type": "int"},
            {"kind": "repeat", "name": "p", "count": count, "sep": "space",
             "elem": {"type": "int"}},
        ]

    def test_n_minus_one(self) -> None:
        self.assertEqual(g.check_spec(self._spec("n-1")), [])
        parsed = g.parse(self._spec("n-1"), "5 3\n1 2 3 4\n")
        self.assertEqual(parsed["p"], [1, 2, 3, 4])

    def test_parenthesised_expression(self) -> None:
        parsed = g.parse(self._spec("(n+m)//2"), "4 4\n1 2 3 4\n")
        self.assertEqual(parsed["p"], [1, 2, 3, 4])

    def test_unknown_variable_rejected(self) -> None:
        self.assertTrue(g.check_spec(self._spec("z-1")))
        with self.assertRaises(g.GrammarError):
            g.parse(self._spec("z-1"), "5 3\n1 2 3 4\n")

    def test_division_by_zero_rejected(self) -> None:
        with self.assertRaises(g.GrammarError):
            g.parse(self._spec("n//0"), "5 3\n1 2 3 4\n")

    def test_expression_must_be_integer(self) -> None:
        # 真除法产生 float，必须拒绝而不是静默截断
        with self.assertRaises(g.GrammarError):
            g.parse(self._spec("n/2"), "5 3\n1 2 3 4\n")

    def test_function_call_rejected(self) -> None:
        self.assertTrue(g.check_spec(self._spec("__import__('os').getpid()")))


if __name__ == "__main__":
    unittest.main()
