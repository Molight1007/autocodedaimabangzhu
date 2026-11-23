"""排版分块的代码门：块必须合法、且覆盖每一行非空正文。

这块最容易出"看起来对、其实错位"的问题：块少一行，正文就会掉进别的小节下面；
块重叠，同一段话会被渲染两遍。所以宁可整题退回自动排版，也不放行。
"""

from __future__ import annotations

import unittest

from autocode.slim2.segment import normalize_layout
from autocode.slim2.validators import check_blocks, check_layout_coverage


class TestCheckBlocks(unittest.TestCase):
    def test_valid_blocks(self) -> None:
        raw = [
            {"kind": "heading", "from": 1, "to": 1},
            {"kind": "para", "from": 2, "to": 5},
            {"kind": "sample_input", "from": 6, "to": 8},
            {"kind": "sample_output", "from": 9, "to": 10},
        ]
        blocks, warnings = check_blocks(raw, 10)
        self.assertEqual(len(blocks), 4)
        self.assertEqual(warnings, [])

    def test_missing_blocks_is_not_an_error(self) -> None:
        """模型没给分块（旧提示词/主动省略）时应安静退回自动排版，不该刷告警。"""
        blocks, warnings = check_blocks(None, 10)
        self.assertIsNone(blocks)
        self.assertEqual(warnings, [])

    def test_unknown_kind_rejected(self) -> None:
        blocks, warnings = check_blocks([{"kind": "figure", "from": 1, "to": 2}], 10)
        self.assertIsNone(blocks)
        self.assertIn("不支持", warnings[0])

    def test_out_of_range_clamped(self) -> None:
        """模型把 end_line 多算两行是常事：裁到边界就行，不该整题作废。"""
        blocks, warnings = check_blocks([{"kind": "para", "from": 1, "to": 99}], 10)
        self.assertEqual(blocks, [{"kind": "para", "from": 1, "to": 10}])
        self.assertIn("越界", warnings[0])

    def test_block_fully_outside_dropped(self) -> None:
        blocks, _ = check_blocks(
            [
                {"kind": "heading", "from": 1, "to": 1},
                {"kind": "para", "from": 11, "to": 20},
            ],
            10,
        )
        self.assertEqual([block["from"] for block in blocks], [1])

    def test_absolute_lines_translated_by_offset(self) -> None:
        """模型给的是原文绝对行号，校验器负责减 start_line。"""
        raw = [
            {"kind": "heading", "from": 162, "to": 162},
            {"kind": "para", "from": 163, "to": 170},
        ]
        blocks, warnings = check_blocks(raw, 141, offset=161)
        self.assertEqual(warnings, [])
        self.assertEqual([(b["from"], b["to"]) for b in blocks], [(1, 1), (2, 9)])

    def test_overfragmented_rejected(self) -> None:
        """每行一个块渲染出来是一堆断句，不如自动排版。"""
        raw = [{"kind": "para", "from": n, "to": n} for n in range(1, 61)]
        blocks, warnings = check_blocks(raw, 60)
        self.assertIsNone(blocks)
        self.assertIn("过碎", warnings[0])

    def test_overlap_rejected(self) -> None:
        raw = [
            {"kind": "heading", "from": 1, "to": 5},
            {"kind": "para", "from": 4, "to": 8},
        ]
        blocks, warnings = check_blocks(raw, 10)
        self.assertIsNone(blocks)
        self.assertIn("重叠", warnings[0])

    def test_same_kind_overlap_merged(self) -> None:
        """模型把一段话拆成首尾相接的两块时，合并而不是整题作废。"""
        raw = [
            {"kind": "para", "from": 1, "to": 5},
            {"kind": "para", "from": 5, "to": 8},
        ]
        blocks, warnings = check_blocks(raw, 10)
        self.assertEqual(warnings, [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual((blocks[0]["from"], blocks[0]["to"]), (1, 8))

    def test_cross_kind_overlap_rejected(self) -> None:
        raw = [
            {"kind": "para", "from": 1, "to": 5},
            {"kind": "heading", "from": 5, "to": 5},
        ]
        self.assertIsNone(check_blocks(raw, 10)[0])

    def test_out_of_order_rejected(self) -> None:
        raw = [
            {"kind": "para", "from": 5, "to": 8},
            {"kind": "heading", "from": 1, "to": 1},
        ]
        self.assertIsNone(check_blocks(raw, 10)[0])

    def test_non_integer_rejected(self) -> None:
        blocks, warnings = check_blocks([{"kind": "para", "from": "一", "to": 3}], 10)
        self.assertIsNone(blocks)
        self.assertTrue(warnings)


class TestCoverage(unittest.TestCase):
    LINES = ["仓库规划（plan）", "【题目描述】", "正文一", "", "正文二", "【子任务】", "对100% 的数据"]

    def test_complete_coverage(self) -> None:
        blocks = [
            {"kind": "heading", "from": 2, "to": 2},
            {"kind": "para", "from": 3, "to": 5},
            {"kind": "heading", "from": 6, "to": 6},
            {"kind": "para", "from": 7, "to": 7},
        ]
        ok, reason = check_layout_coverage(blocks, self.LINES)
        self.assertTrue(ok, reason)

    def test_title_line_may_be_uncovered(self) -> None:
        blocks = [{"kind": "para", "from": 2, "to": 7}]
        ok, _ = check_layout_coverage(blocks, self.LINES)
        self.assertTrue(ok)

    def test_blank_line_may_be_uncovered(self) -> None:
        blocks = [
            {"kind": "para", "from": 2, "to": 3},
            {"kind": "para", "from": 5, "to": 7},
        ]
        ok, _ = check_layout_coverage(blocks, self.LINES)
        self.assertTrue(ok)

    def test_missing_prose_line_detected(self) -> None:
        blocks = [
            {"kind": "para", "from": 2, "to": 3},
            {"kind": "para", "from": 5, "to": 7},
        ]
        lines = self.LINES[:]
        lines[3] = "这一行是正文，不能被漏掉"
        ok, reason = check_layout_coverage(blocks, lines)
        self.assertFalse(ok)
        self.assertIn("没被任何分块覆盖", reason)




class TestNormalizeLayout(unittest.TestCase):
    """补缝与合并：模型漏行不能丢字，碎块不能渲染成一堆断句。"""

    LINES = ["题名", "【题目描述】", "第一句", "第二句", "", "第三句", "【子任务】", "数据范围", "尾行"]

    def test_gap_filled_as_paragraph(self) -> None:
        blocks = [
            {"kind": "heading", "from": 2, "to": 2},
            {"kind": "para", "from": 3, "to": 3},
            {"kind": "heading", "from": 7, "to": 7},
        ]
        result, inserted = normalize_layout(blocks, self.LINES)
        self.assertGreater(inserted, 0)
        covered = set()
        for block in result:
            covered.update(range(block["from"], block["to"] + 1))
        for number, line in enumerate(self.LINES, start=1):
            if number > 1 and line.strip():
                self.assertIn(number, covered, f"第 {number} 行仍然没有块覆盖")

    def test_adjacent_paragraphs_merged(self) -> None:
        blocks = [
            {"kind": "para", "from": 2, "to": 2},
            {"kind": "para", "from": 3, "to": 4},
            {"kind": "para", "from": 5, "to": 9},
        ]
        result, _ = normalize_layout(blocks, self.LINES)
        paras = [block for block in result if block["kind"] == "para"]
        self.assertEqual(len(paras), 1, "相邻段落必须合成一块")
        self.assertEqual(paras[0]["from"], 2)
        self.assertEqual(paras[0]["to"], 9)

    def test_different_kind_not_merged(self) -> None:
        blocks = [
            {"kind": "para", "from": 2, "to": 3},
            {"kind": "list", "from": 4, "to": 5},
        ]
        result, _ = normalize_layout(blocks, self.LINES)
        self.assertEqual(result[0]["kind"], "para")
        self.assertEqual(result[1]["kind"], "list", "不同类别的相邻块不能合并")
        self.assertEqual((result[0]["from"], result[0]["to"]), (2, 3))


if __name__ == "__main__":
    unittest.main()
