"""比较器测试：exact_tokens / line_trim / float_eps 的边界。"""

from __future__ import annotations

import unittest

from autocode.engine.verify.comparator import (
    CHECKER_EXACT,
    CHECKER_FLOAT_EPS,
    CHECKER_LINE_TRIM,
    compare,
    compare_exact_tokens,
    compare_float_eps,
    compare_line_trim,
    tokenize,
)


class TestExactTokens(unittest.TestCase):
    def test_trailing_whitespace_ignored(self) -> None:
        self.assertTrue(compare_exact_tokens("1 3\n2 1\n", "1 3  \n2 1\n").ok)

    def test_crlf_ignored(self) -> None:
        """Windows 上 Python 的 print 会输出 \\r\\n，判题不能被它坑。"""
        self.assertTrue(compare_exact_tokens("1\n2\n", "1\r\n2\r\n").ok)

    def test_value_mismatch_reports_index(self) -> None:
        result = compare_exact_tokens("1 2 3", "1 9 3")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "token_mismatch")
        self.assertEqual(result.detail["first_diff_index"], 1)

    def test_missing_line_detected(self) -> None:
        result = compare_exact_tokens("1\n2\n", "1\n")
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["expected_tokens"], 2)
        self.assertEqual(result.detail["actual_tokens"], 1)


class TestLineTrim(unittest.TestCase):
    def test_line_count_must_match(self) -> None:
        self.assertFalse(compare_line_trim("1 2\n3 4\n", "1 2 3 4\n").ok)
        self.assertTrue(compare_line_trim("1 2\n3 4\n", "1 2\n3 4").ok)

    def test_first_diff_line_is_one_based(self) -> None:
        result = compare_line_trim("a\nb\n", "a\nc\n")
        self.assertEqual(result.detail["first_diff_line"], 2)


class TestFloatEps(unittest.TestCase):
    def test_within_eps(self) -> None:
        self.assertTrue(compare_float_eps("3.14159", "3.1415926", eps=1e-4).ok)

    def test_out_of_eps(self) -> None:
        result = compare_float_eps("1.0", "1.1", eps=1e-6)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "float_mismatch")

    def test_token_count_mismatch(self) -> None:
        self.assertEqual(compare_float_eps("1 2", "1 2 3").reason, "token_count_mismatch")

    def test_nan_is_not_equal(self) -> None:
        self.assertFalse(compare_float_eps("nan", "nan").ok)


class TestDispatch(unittest.TestCase):
    def test_unknown_checker_raises(self) -> None:
        with self.assertRaises(ValueError):
            compare("fuzzy", "1", "1")

    def test_missing_expected(self) -> None:
        result = compare(CHECKER_EXACT, None, "1")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "expected_output_missing")

    def test_all_checkers_dispatch(self) -> None:
        for checker in (CHECKER_EXACT, CHECKER_LINE_TRIM, CHECKER_FLOAT_EPS):
            self.assertTrue(compare(checker, "1\n2\n", "1\n2\n").ok, checker)

    def test_tokenize(self) -> None:
        self.assertEqual(tokenize(" 1  2\r\n3 "), ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
