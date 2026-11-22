"""两段式导入的契约：前端先抽取、再切题，图块坐标必须完整传回切题阶段。

这个契约一旦断了，症状很隐蔽——题能切出来、也能入库，只是永远没有插图，
界面上看不出任何报错。所以用测试把它焊死。
"""

from __future__ import annotations

import unittest
from unittest import mock

from autocode.slim2 import api, segment

FIGURES = [{"id": "fig_1", "page": 6, "bbox": [192.6, 395.5, 402.6, 591.2], "width": 210, "height": 196}]
RANGES = [[1, 10], [11, 20]]


def _capture(result=None):
    """把 segment_text 换成记录器，返回 (调用记录, 打桩函数)。"""
    captured = {}

    def fake(db, runner, text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return result or {"ok": True, "batch_id": 1, "segments": [], "warnings": []}

    return captured, fake


class TestImportTextForwarding(unittest.TestCase):
    def test_figure_payload_forwarded(self) -> None:
        captured, fake = _capture()
        with mock.patch.object(segment, "segment_text", fake):
            result = api.import_text(
                None,
                None,
                {
                    "text": "题目一 仓库规划\n【题目描述】给定一棵树。",
                    "source_name": "CSP32-statement.pdf",
                    "kind": "file",
                    "figures": FIGURES,
                    "page_ranges": RANGES,
                    "pdf_key": "abc123",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured["kind"], "file")
        self.assertEqual(captured["figures"], FIGURES)
        self.assertEqual(captured["page_ranges"], RANGES)
        self.assertEqual(captured["pdf_key"], "abc123")
        self.assertEqual(captured["source_name"], "CSP32-statement.pdf")

    def test_text_only_import_stays_plain(self) -> None:
        captured, fake = _capture()
        with mock.patch.object(segment, "segment_text", fake):
            api.import_text(None, None, {"text": "题目一 求最大值"})
        self.assertIsNone(captured["figures"])
        self.assertIsNone(captured["page_ranges"])
        self.assertEqual(captured["pdf_key"], "")
        self.assertEqual(captured["kind"], "text")

    def test_malformed_figures_ignored(self) -> None:
        """前端传了非列表（旧版本或手抖）时不能把切题带崩。"""
        captured, fake = _capture()
        with mock.patch.object(segment, "segment_text", fake):
            api.import_text(
                None, None, {"text": "题目一", "figures": "oops", "page_ranges": {"a": 1}}
            )
        self.assertIsNone(captured["figures"])
        self.assertIsNone(captured["page_ranges"])

    def test_empty_text_rejected(self) -> None:
        _, fake = _capture()
        with mock.patch.object(segment, "segment_text", fake):
            result = api.import_text(None, None, {"text": "   "})
        self.assertEqual(result["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
