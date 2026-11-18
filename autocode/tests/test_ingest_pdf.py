"""整卷 PDF 导入测试：LLM 只标边界，题面/样例必须来自原文。

关键回归：
- 模型给的 start_line 落在页眉「第 N 次认证…」上时，代码要吸附到正式标题；
- 越界/重叠/过短区间一律丢弃，不能把错误边界带进库；
- 没有完整样例的题不入库（宁可跳过，也不生成无样例的题）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from autocode.app.ingest_pdf import (
    Segment,
    build_sections,
    extract_pdf_text,
    ingest_pdf_text,
    parse_segments,
    segment_with_llm,
)
from autocode.llm.client import ChatResult, ChatUsage
from autocode.store.db import Database

TEXT = """CCF CSP 计算机软件能力认证
第99 次认证
第99 次认证仓库规划（plan）
仓库规划（plan）
【题目描述】
给定 n 个仓库，求距离。
【输入格式】
第一行一个整数 n。
【样例输入】
3
1 2 3
【样例输出】
6
【样例解释】
1+2+3=6。
因子化简（prime）
【题目描述】
给定 n 与 k，化简质因子。
【输入格式】
第一行一个整数 q。
【样例输入】
1
100 3
【样例输出】
100
【样例解释】
略。
"""


class StubClient:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls = []

    def chat(self, messages, **kwargs) -> ChatResult:
        self.calls.append({"messages": messages, **kwargs})
        return ChatResult(
            content=json.dumps(self.payload, ensure_ascii=False),
            usage=ChatUsage(10, 5, 15),
            attempts=1,
            elapsed_ms=1.0,
            model="stub",
            json_obj=self.payload,
            finish_reason="stop",
        )


def _line(text: str, needle: str, *, nth: int = 0) -> int:
    hits = [index + 1 for index, line in enumerate(text.splitlines()) if needle in line]
    return hits[nth]


class TestParseSegments(unittest.TestCase):
    def test_overlap_and_out_of_range_dropped(self) -> None:
        payload = {"problems": [
            {"title": "A", "slug": "a", "start_line": 1, "end_line": 10},
            {"title": "B", "slug": "b", "start_line": 8, "end_line": 20},     # 重叠
            {"title": "C", "slug": "c", "start_line": 30, "end_line": 40},    # 越界
            {"title": "D", "slug": "d", "start_line": 5, "end_line": 6},      # 过短
        ]}
        segments, warnings = parse_segments(payload, 25)
        self.assertEqual([s.slug for s in segments], ["a"])
        self.assertEqual(len(warnings), 3, warnings)

    def test_non_integer_lines_dropped(self) -> None:
        segments, warnings = parse_segments(
            {"problems": [{"title": "A", "slug": "a", "start_line": "x", "end_line": 9}]}, 20
        )
        self.assertEqual(segments, [])
        self.assertTrue(any("不是整数" in item for item in warnings))


class TestSegmentation(unittest.TestCase):
    def test_header_line_snapped_to_real_title(self) -> None:
        """模型指到页眉「第99次认证仓库规划（plan）」时，要向后吸附到正式标题。"""
        header = _line(TEXT, "第99 次认证仓库规划", nth=0)
        real = _line(TEXT, "仓库规划（plan）", nth=1)
        payload = {"problems": [{"title": "仓库规划", "slug": "plan",
                                 "start_line": header, "end_line": real + 15}]}
        client = StubClient(payload)
        segments, _ = segment_with_llm(client, TEXT)
        lines = TEXT.splitlines()
        sections, skipped = build_sections(lines, segments, source_id_prefix="demo")
        self.assertFalse(skipped)
        self.assertEqual(sections[0].lines[0].strip(), "仓库规划（plan）")
        self.assertEqual(len(client.calls), 1)

    def test_no_segments_falls_back_to_title_lines(self) -> None:
        client = StubClient({"problems": []})
        segments, warnings = segment_with_llm(client, TEXT)
        self.assertEqual([s.slug for s in segments], ["plan", "prime"])
        self.assertTrue(any("兜底" in item for item in warnings))


class TestIngest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "pdf.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_ingest_uses_verbatim_slice_and_keeps_samples(self) -> None:
        start1 = _line(TEXT, "仓库规划（plan）", nth=1)
        start2 = _line(TEXT, "因子化简（prime）")
        payload = {"problems": [
            {"title": "仓库规划", "slug": "plan", "start_line": start1, "end_line": start2 - 1},
            {"title": "因子化简", "slug": "prime", "start_line": start2, "end_line": len(TEXT.splitlines())},
        ]}
        result = ingest_pdf_text(TEXT, db=self.db, client=StubClient(payload), path_label="demo.pdf")
        self.assertEqual(len(result.report.problems), 2, result.to_dict())

        rows = {row["title"]: row for row in self.db.query_all(
            "SELECT id, title, statement_md FROM problems ORDER BY id"
        )}
        self.assertEqual(set(rows), {"仓库规划", "因子化简"})
        # 题面逐字来自原文：样例值必须原样存在，且不含下一个题的标题
        self.assertIn("3\n1 2 3", rows["仓库规划"]["statement_md"])
        self.assertNotIn("因子化简（prime）", rows["仓库规划"]["statement_md"])
        samples = self.db.query_all(
            "SELECT input_data, expected_output FROM test_cases WHERE problem_id=? AND layer='sample'",
            (rows["仓库规划"]["id"],),
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["input_data"].strip(), "3\n1 2 3")
        self.assertEqual(samples[0]["expected_output"].strip(), "6")

    def test_problem_without_samples_is_skipped(self) -> None:
        broken = "仓库规划（plan）\n【题目描述】\n只有描述，没有样例。\n"
        payload = {"problems": [{"title": "仓库规划", "slug": "plan",
                                 "start_line": 1, "end_line": 3}]}
        result = ingest_pdf_text(broken, db=self.db, client=StubClient(payload), path_label="x.pdf")
        self.assertEqual(result.report.problems, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("未抽到完整样例", result.skipped[0]["reason"])
        self.assertEqual(int(self.db.query_scalar("SELECT COUNT(*) FROM problems", (), 0)), 0)

    def test_incomplete_extra_sample_is_not_stored(self) -> None:
        """一组完整 + 一组缺输出：只入库完整那组，不能把空输出写进 spec。"""
        text = (
            "题A（a）\n【题目描述】\n描述\n"
            "【样例1 输入】\n3\n1 2 3\n【样例1 输出】\n6\n"
            "【样例2 输入】\n5\n1 2 3 4 5\n【样例2 输出】\n\n"
        )
        payload = {"problems": [{"title": "题A", "slug": "a",
                                 "start_line": 1, "end_line": len(text.splitlines())}]}
        result = ingest_pdf_text(text, db=self.db, client=StubClient(payload), path_label="x.pdf")
        self.assertEqual(len(result.report.problems), 1, result.to_dict())
        row = self.db.query_one("SELECT id, spec_json FROM problems")
        samples = (json.loads(row["spec_json"] or "{}").get("samples") or [])
        self.assertEqual(len(samples), 1, samples)
        self.assertTrue(samples[0]["input"] and samples[0]["output"])
        cases = self.db.query_all(
            "SELECT input_data, expected_output FROM test_cases WHERE problem_id=?", (row["id"],)
        )
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["expected_output"].strip())


class TestPdfExtraction(unittest.TestCase):
    def test_extract_text_from_real_pdf(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "t.pdf"
            doc = pymupdf.open()
            page = doc.new_page()
            # 默认字体不含中文，这里只验证“文本抽取通路”，用 ASCII 内容
            page.insert_text((72, 72), "Warehouse (plan)")
            doc.save(str(path))
            doc.close()
            self.assertIn("Warehouse", extract_pdf_text(path))


class TestImportPdfApi(unittest.TestCase):
    """API 层只做入参与路径校验；不触发 LLM。"""

    def test_quoted_path_is_stripped(self) -> None:
        from autocode.web import api

        result = api.import_pdf({"path": '"C:\\nope\\x.pdf"'})
        self.assertIn("文件不存在", result["error"])
        self.assertNotIn('"', result["error"])

    def test_non_pdf_rejected(self) -> None:
        from autocode.web import api

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("x", encoding="utf-8")
            result = api.import_pdf({"path": str(path)})
        self.assertEqual(result["error"], "只支持 .pdf 文件")

    def test_empty_path_rejected(self) -> None:
        from autocode.web import api

        self.assertEqual(api.import_pdf({"path": "  "})["error"], "请填写 PDF 的绝对路径")


class TestWarningsDeduped(unittest.TestCase):
    def test_same_warning_not_printed_twice(self) -> None:
        from autocode.app.ingest_csp import IngestReport
        from autocode.app.ingest_pdf import PdfIngestResult

        result = PdfIngestResult(report=IngestReport(source="x", path="x.pdf"), warnings=["a"])
        result.report.warnings.extend(["a", "b"])
        self.assertEqual(result.to_dict()["warnings"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
