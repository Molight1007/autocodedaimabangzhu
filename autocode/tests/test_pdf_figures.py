"""图块枚举与多模态消息的纯函数测试（不需要联网、不需要真实试卷）。

覆盖三处容易回归的地方：
1. 矢量路径聚类：一张树图的几十条路径要合成一块，页边线要能被判掉；
2. 正文误判：图块里压着成段文字时不能被当成插图；
3. 模型返回的 figure_ids / after_line 必须校验，不能把编造的编号写进库。
"""

from __future__ import annotations

import unittest

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore

from autocode.app.ingest_pdf import parse_segments
from autocode.app.pdf_figures import (
    _looks_like_text,
    _shape_count,
    cluster_rects,
    page_line_ranges,
)
from autocode.llm.messages import has_image_blocks, strip_image_blocks


class TestCluster(unittest.TestCase):
    def test_near_rects_merge_far_rects_split(self) -> None:
        near_a = pymupdf.Rect(0, 0, 20, 20)
        near_b = pymupdf.Rect(25, 0, 45, 20)  # 间距 5pt < gap
        far = pymupdf.Rect(200, 0, 220, 20)
        clusters = cluster_rects([near_a, near_b, far], gap=14)
        self.assertEqual(len(clusters), 2)
        widths = sorted(float(rect.width) for rect, _ in clusters)
        self.assertAlmostEqual(widths[0], 20.0, places=1)
        self.assertAlmostEqual(widths[1], 45.0, places=1)

    def test_vertical_gap_still_merges(self) -> None:
        upper = pymupdf.Rect(0, 0, 40, 20)
        lower = pymupdf.Rect(10, 30, 50, 50)  # 纵向间距 10pt
        clusters = cluster_rects([upper, lower], gap=14)
        self.assertEqual(len(clusters), 1)
        self.assertAlmostEqual(float(clusters[0][0].height), 50.0, places=1)

    def test_shape_count_ignores_hairlines(self) -> None:
        lines = [pymupdf.Rect(0, 0, 400, 1) for _ in range(6)]  # 分隔线
        self.assertEqual(_shape_count(lines), 0)
        boxes = [pymupdf.Rect(i * 10, 0, i * 10 + 20, 20) for i in range(3)]
        self.assertEqual(_shape_count(boxes), 3)


class TestTextGuard(unittest.TestCase):
    def test_long_text_line_rejected(self) -> None:
        rect = pymupdf.Rect(0, 0, 300, 200)
        lines = [(pymupdf.Rect(10, 10, 290, 30), 60)]
        self.assertTrue(_looks_like_text(rect, lines))

    def test_short_node_labels_allowed(self) -> None:
        rect = pymupdf.Rect(0, 0, 300, 200)
        lines = [(pymupdf.Rect(10, 10, 30, 30), 2), (pymupdf.Rect(50, 10, 70, 30), 1)]
        self.assertFalse(_looks_like_text(rect, lines))

    def test_text_outside_block_ignored(self) -> None:
        rect = pymupdf.Rect(0, 0, 100, 100)
        lines = [(pymupdf.Rect(200, 200, 380, 220), 60)]
        self.assertFalse(_looks_like_text(rect, lines))


class TestParseSegmentsFigures(unittest.TestCase):
    def test_unknown_figure_id_dropped_with_warning(self) -> None:
        payload = {
            "problems": [
                {
                    "title": "A",
                    "slug": "a",
                    "start_line": 1,
                    "end_line": 10,
                    "figure_ids": ["fig_1", "fig_99"],
                    "after_line": 5,
                }
            ]
        }
        segments, warnings = parse_segments(payload, 20, valid_figures=["fig_1"])
        self.assertEqual(segments[0].figure_ids, ["fig_1"])
        self.assertTrue(any("fig_99" in item for item in warnings), warnings)

    def test_after_line_out_of_range_reset(self) -> None:
        payload = {
            "problems": [
                {"title": "A", "slug": "a", "start_line": 5, "end_line": 10, "after_line": 99}
            ]
        }
        segments, _ = parse_segments(payload, 20)
        self.assertEqual(segments[0].after_line, 0)

    def test_duplicate_figure_ids_collapsed(self) -> None:
        payload = {
            "problems": [
                {
                    "title": "A",
                    "slug": "a",
                    "start_line": 1,
                    "end_line": 10,
                    "figure_ids": ["fig_1", "fig_1"],
                }
            ]
        }
        segments, _ = parse_segments(payload, 20, valid_figures=["fig_1"])
        self.assertEqual(segments[0].figure_ids, ["fig_1"])

    def test_per_figure_positions(self) -> None:
        """新格式 figures 每张图带独立位置；越界位置被丢弃。"""
        payload = {
            "problems": [
                {
                    "title": "A",
                    "slug": "a",
                    "start_line": 10,
                    "end_line": 40,
                    "figures": [
                        {"id": "fig_1", "after_line": 20},
                        {"id": "fig_2", "after_line": 999},  # 越界
                    ],
                }
            ]
        }
        segments, _ = parse_segments(payload, 50, valid_figures=["fig_1", "fig_2"])
        self.assertEqual(segments[0].figure_ids, ["fig_1", "fig_2"])
        self.assertEqual(segments[0].figure_positions, {"fig_1": 20})
        # 越界的那张退回题目统一位置（这里没给 after_line → 0，由调用方补 1）
        self.assertNotIn("fig_2", segments[0].figure_positions)

    def test_old_and_new_format_merge(self) -> None:
        payload = {
            "problems": [
                {
                    "title": "A",
                    "slug": "a",
                    "start_line": 1,
                    "end_line": 20,
                    "figures": [{"id": "fig_1", "after_line": 5}],
                    "figure_ids": ["fig_1", "fig_2"],
                }
            ]
        }
        segments, _ = parse_segments(payload, 30, valid_figures=["fig_1", "fig_2"])
        self.assertEqual(segments[0].figure_ids, ["fig_1", "fig_2"])
        self.assertEqual(segments[0].figure_positions, {"fig_1": 5})


class TestPageLineRanges(unittest.TestCase):
    """页-行映射必须和 `"\\n".join(页文本).splitlines()` 严格对齐。

    这里容易错：`join` 会在页间多插一个换行，直接累加每页 splitlines 数量会越算越偏。
    """

    def _doc(self):
        doc = pymupdf.open()
        for texts in (("alpha", "beta"), ("gamma", "delta", "epsilon"), ("zeta",)):
            page = doc.new_page()
            for index, text in enumerate(texts):
                page.insert_text((72, 72 + index * 20), text)
        return doc

    def test_first_and_last_line_match_page_text(self) -> None:
        with self._doc() as doc:
            full = "\n".join(page.get_text() for page in doc)
            lines = full.splitlines()
            ranges = page_line_ranges(doc)
        self.assertEqual(len(ranges), 3)
        for index, expected_first in enumerate(("alpha", "gamma", "zeta")):
            start, _end = ranges[index]
            self.assertIn(expected_first, lines[start - 1])

    def test_ranges_are_ordered_and_within_total(self) -> None:
        with self._doc() as doc:
            full = "\n".join(page.get_text() for page in doc)
            ranges = page_line_ranges(doc)
        total = len(full.splitlines())
        for start, end in ranges:
            self.assertLessEqual(1, start)
            self.assertLessEqual(start, end)
            self.assertLessEqual(end, total)
        for previous, current in zip(ranges, ranges[1:]):
            self.assertLess(previous[0], current[0])


    def test_adjacent_boundary_trimmed_not_dropped(self) -> None:
        """模型把下一题标题算进上一题（end == 下题 start）时，只收一格，不丢整题。"""
        payload = {
            "problems": [
                {"title": "A", "slug": "a", "start_line": 1, "end_line": 10},
                {"title": "B", "slug": "b", "start_line": 10, "end_line": 20},
            ]
        }
        segments, warnings = parse_segments(payload, 25)
        self.assertEqual([s.slug for s in segments], ["a", "b"])
        self.assertEqual(segments[0].end_line, 9)
        self.assertEqual(segments[1].start_line, 10)
        self.assertTrue(any("让给下一题" in item for item in warnings), warnings)

    def test_true_overlap_still_dropped(self) -> None:
        payload = {
            "problems": [
                {"title": "A", "slug": "a", "start_line": 1, "end_line": 12},
                {"title": "B", "slug": "b", "start_line": 10, "end_line": 20},
            ]
        }
        segments, warnings = parse_segments(payload, 25)
        self.assertEqual([s.slug for s in segments], ["a"])
        self.assertTrue(any("重叠" in item for item in warnings), warnings)


class TestImageBlocks(unittest.TestCase):
    def test_strip_keeps_text_only(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
                ],
            }
        ]
        self.assertTrue(has_image_blocks(messages))
        stripped = strip_image_blocks(messages)
        self.assertFalse(has_image_blocks(stripped))
        self.assertEqual(stripped[0]["content"], "看这张图")

    def test_plain_string_untouched(self) -> None:
        messages = [{"role": "user", "content": "纯文本"}]
        self.assertFalse(has_image_blocks(messages))
        self.assertEqual(strip_image_blocks(messages), messages)


if __name__ == "__main__":
    unittest.main()
