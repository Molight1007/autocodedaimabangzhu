"""图块插入位置的单元测试。

这里的断言都来自真实踩过的坑：
- CSP32 第 6 页的树图画在下半页，只按页码算会插到题面第 1 行（标题下面）
- CSP32 第 8 页的图紧跟在 `…标注在了图上。` 之后，那行只是折行的尾巴
- CSP32 第 9 页的图在页面最上方，上方只有页眉，应插在本页第一行之前
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autocode.slim2 import files
from autocode.slim2.segment import attach_figures_by_page


def _build_pdf(*, text_ys: list[float], figure_top: float) -> bytes:
    """造一页 PDF：若干行文字 + 一个由 4 个矩形组成的"图"。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for index, y in enumerate(text_ys):
        page.insert_text((72, y), f"LINE {index + 1} content", fontsize=11)
    # 4 个矩形聚成一簇；min_side=36、MIN_SHAPES=3 才认作图块
    for offset in range(4):
        rect = pymupdf.Rect(72, figure_top + offset * 20, 300, figure_top + 40 + offset * 20)
        page.draw_rect(rect, color=(0, 0, 0), width=1)
    raw = doc.tobytes()
    doc.close()
    return raw


class TestAnchorLine(unittest.TestCase):
    def test_figure_below_text_anchors_to_last_line_above(self) -> None:
        raw = _build_pdf(text_ys=[80, 110, 140], figure_top=200)
        bundle = files.extract_pdf_bundle(raw)
        self.assertIsNotNone(bundle)
        figures = bundle["figures"]
        self.assertEqual(len(figures), 1, "应当识别出一个图块")
        # 三行文字 + 图为第 4 个块；图在第三行下方，所以锚点是第 3 行
        self.assertEqual(figures[0]["anchor_line"], 3)

    def test_furniture_line_does_not_shift_anchor(self) -> None:
        # 用合成数据直接测：pymupdf 内置字体画不出中文，合成 PDF 里的页眉会变成 "·"，
        # 测不到"剔除页眉后行号回退"这条规则。
        raw = ["第 1 页 共 16 页", "LINE 1 content", "LINE 2 content"]
        boxes = [
            (30.0, 42.0, "第 1 页 共 16 页"),
            (68.0, 83.0, "LINE 1 content"),
            (98.0, 113.0, "LINE 2 content"),
        ]
        lines, coords = files._page_layout(raw, boxes, [])
        self.assertEqual(lines, ["LINE 1 content", "LINE 2 content"], "页眉必须删掉")
        self.assertEqual(coords, [(68.0, 83.0), (98.0, 113.0)])

    def test_text_inside_figure_is_dropped(self) -> None:
        """图里的标签不能留在题面：实测会把一句话劈成两半。"""
        raw = ["前一句", "1, 10", "No", "后一句"]
        boxes = [
            (60.0, 72.0, "前一句"),
            (90.0, 100.0, "1, 10"),
            (110.0, 120.0, "No"),
            (200.0, 212.0, "后一句"),
        ]
        lines, _ = files._page_layout(raw, boxes, [(85.0, 125.0)])
        self.assertEqual(lines, ["前一句", "后一句"])

    def test_text_beside_figure_is_kept(self) -> None:
        """图只占半页时，旁边的正文不能被误删。"""
        raw = ["正文一行", "图侧一行"]
        boxes = [(60.0, 72.0, "正文一行"), (300.0, 312.0, "图侧一行")]
        lines, _ = files._page_layout(raw, boxes, [(65.0, 200.0)])
        self.assertEqual(lines, ["正文一行", "图侧一行"])

    def test_figure_at_page_top_has_no_anchor(self) -> None:
        raw = _build_pdf(text_ys=[400, 430], figure_top=80)
        bundle = files.extract_pdf_bundle(raw)
        self.assertEqual(bundle["figures"][0]["anchor_line"], 0, "上方无正文时不该瞎猜")


class TestAttachFigures(unittest.TestCase):
    def _bundle(self) -> dict:
        raw = _build_pdf(text_ys=[80, 110, 140], figure_top=200)
        return files.extract_pdf_bundle(raw)

    def test_relative_line_uses_anchor(self) -> None:
        bundle = self._bundle()
        statement = "\n".join(f"第 {n} 行" for n in range(1, 9))
        rows = [{"start_line": 1, "end_line": 8, "statement_md": statement}]
        attach_figures_by_page(rows, bundle["figures"], bundle["page_ranges"])
        # 锚点在第 3 行 → 题面内第 3 行之后
        self.assertEqual(rows[0]["figures"][0]["after_line"], 3)

    def test_relative_line_clamped_to_statement(self) -> None:
        bundle = self._bundle()
        rows = [{"start_line": 3, "end_line": 4, "statement_md": "甲\n乙"}]  # 只有 2 行
        attach_figures_by_page(rows, bundle["figures"], bundle["page_ranges"])
        after = rows[0]["figures"][0]["after_line"]
        self.assertGreaterEqual(after, 1)
        self.assertLessEqual(after, 2, "插到题面之外，前端会把它丢在末尾")

    def test_page_top_figure_goes_before_page_first_line(self) -> None:
        raw = _build_pdf(text_ys=[400, 430], figure_top=80)
        bundle = files.extract_pdf_bundle(raw)
        statement = "\n".join(f"第 {n} 行" for n in range(1, 9))
        rows = [{"start_line": 1, "end_line": 8, "statement_md": statement}]
        attach_figures_by_page(rows, bundle["figures"], bundle["page_ranges"])
        # 单页文档的页首就是题首，回退到 1
        self.assertEqual(rows[0]["figures"][0]["after_line"], 1)

    def test_figures_stay_increasing(self) -> None:
        bundle = self._bundle()
        figures = bundle["figures"] * 3  # 三张同一位置的图
        for index, figure in enumerate(figures):
            figures[index] = dict(figure, id=f"fig_{index + 1}")
        statement = "\n".join(f"第 {n} 行" for n in range(1, 9))
        rows = [{"start_line": 1, "end_line": 8, "statement_md": statement}]
        attach_figures_by_page(rows, figures, bundle["page_ranges"])
        afters = [item["after_line"] for item in rows[0]["figures"]]
        self.assertEqual(afters, sorted(set(afters)), "同题多图必须严格递增")


if __name__ == "__main__":
    unittest.main()
