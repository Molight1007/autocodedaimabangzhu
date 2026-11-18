"""通用版式修复：不针对具体题目，只处理 PDF 抽取的四类系统性损坏。"""

from __future__ import annotations

import unittest

from autocode.app.layout_repair import (
    normalize_chars,
    restore_merged_lines,
    sanitize_sample,
    strip_number_column,
)


class TestLayoutRepair(unittest.TestCase):
    def test_normalize_unicode_minus(self) -> None:
        self.assertEqual(normalize_chars("‐5 ‑3 −1"), "-5 -3 -1")

    def test_strip_alternating_numbers(self) -> None:
        lines = ["1", "6", "2", "1 a hello", "3", "1 b world", "4", "3 c"]
        cleaned, hit = strip_number_column(lines)
        self.assertTrue(hit)
        self.assertEqual(cleaned, ["6", "1 a hello", "1 b world", "3 c"])

    def test_strip_prefix_numbers(self) -> None:
        lines = ["1 6", "2 1 a hello", "3 1 b world", "4 3 c"]
        cleaned, hit = strip_number_column(lines)
        self.assertTrue(hit)
        self.assertEqual(cleaned, ["6", "1 a hello", "1 b world", "3 c"])

    def test_clean_sample_untouched(self) -> None:
        inp, out, notes = sanitize_sample("5\n10 2 6 6 8\n", "586\n")
        self.assertEqual(inp, "5\n10 2 6 6 8\n")
        self.assertEqual(out, "586\n")
        self.assertEqual(notes, [])

    def test_merged_two_array_restored(self) -> None:
        inp, out, notes = sanitize_sample(
            "1 9 7 2 Par les soirs bleus dete jirai dans les sentiers 3 PICOTE PAR LES BLES FOULER LHERBE MENUE\n",
            "2\n13\n",
        )
        self.assertIn("9 7\nPar les soirs bleus dete jirai dans les sentiers", inp)
        self.assertIn("PICOTE PAR LES BLES", inp)
        self.assertIn("merged_lines_restored", notes)

    def test_two_line_input_not_misread_as_merged(self) -> None:
        """两行正常数据不能被当成“压平行”：`5 2` 里的 m=2 不是行号。"""
        text = "5 2\n10 50 10 10 20\n"
        restored, hit = restore_merged_lines(text)
        self.assertFalse(hit)
        self.assertEqual(restored, text)
        inp, _, _ = sanitize_sample(text, "586\n")
        self.assertEqual(inp, text)

    def test_output_line_number_stripped_with_input(self) -> None:
        inp, out, notes = sanitize_sample("1\n4\n2\n1 2 3 4\n", "1\n10\n")
        self.assertEqual(inp, "4\n1 2 3 4\n")
        self.assertEqual(out, "10\n")
        self.assertIn("line_number_column_stripped", notes)

    def test_output_single_line_number_stripped(self) -> None:
        inp, out, notes = sanitize_sample("1 2\n3 5\n504\n", "1\n101\n")
        self.assertEqual(out, "101\n")
        self.assertIn("line_number_column_stripped", notes)

    def test_page_artifacts_dropped(self) -> None:
        inp, out, _ = sanitize_sample(
            "4\n1 2 3 4\n第8 页\n共15 页\nCCF CSP 计算机软件能力认证\n",
            "10\n",
        )
        self.assertEqual(inp, "4\n1 2 3 4\n")


if __name__ == "__main__":
    unittest.main()
