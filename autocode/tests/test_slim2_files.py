"""文件抽取（PDF / DOCX / 文本）的契约测试。

重点不是"能抽出字"，而是**出错时给人话**：
上传入口是唯一直接面向用户的文件通道，报 500 或者吐乱码都会被当成"这系统坏了"。
"""

from __future__ import annotations

import base64
import io
import unittest

from autocode.slim2 import files


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


SAMPLE = "题目一 区间最大值\n【题目描述】\n给定 n 个整数，求最大值。\n" * 3


class TestPlainText(unittest.TestCase):
    def test_utf8(self) -> None:
        name, raw = files.decode_upload({"filename": "a.txt", "content_b64": _b64(SAMPLE.encode("utf-8"))})
        self.assertEqual(name, "a.txt")
        self.assertIn("区间最大值", files.extract_text(name, raw))

    def test_utf8_bom(self) -> None:
        raw = b"\xef\xbb\xbf" + SAMPLE.encode("utf-8")
        self.assertIn("区间最大值", files.extract_text("a.txt", raw))

    def test_gbk(self) -> None:
        raw = SAMPLE.encode("gb18030")
        self.assertIn("区间最大值", files.extract_text("a.txt", raw))

    def test_crlf_normalized(self) -> None:
        raw = SAMPLE.replace("\n", "\r\n").encode("utf-8")
        text = files.extract_text("a.txt", raw)
        self.assertNotIn("\r", text)
        self.assertIn("区间最大值", text)


class TestRejections(unittest.TestCase):
    def test_empty_payload(self) -> None:
        with self.assertRaises(files.FileError) as ctx:
            files.decode_upload({"filename": "a.txt", "content_b64": ""})
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_too_large(self) -> None:
        with self.assertRaises(files.FileError) as ctx:
            files.decode_upload({
                "filename": "a.txt",
                "content_b64": _b64(b"x" * (files.MAX_BYTES + 10)),
            })
        self.assertEqual(ctx.exception.code, "TOO_LARGE")

    def test_too_short(self) -> None:
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("a.txt", "太短".encode("utf-8"))
        self.assertEqual(ctx.exception.code, "TOO_SHORT")

    def test_binary_rejected(self) -> None:
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("旧格式.doc", b"\x00\x01\x02" * 100)
        self.assertEqual(ctx.exception.code, "BINARY_FILE")

    def test_broken_pdf(self) -> None:
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("a.pdf", b"definitely not a pdf" * 10)
        self.assertIn(ctx.exception.code, ("BAD_PDF", "NO_PDF_ENGINE"))


@unittest.skipUnless(_has("pymupdf"), "本机没有 pymupdf")
class TestPdf(unittest.TestCase):
    def _pdf(self) -> bytes:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        y = 80
        for line in SAMPLE.splitlines():
            page.insert_text((60, y), line, fontname="china-s", fontsize=12)
            y += 22
        data = doc.tobytes()
        doc.close()
        return data

    def test_chinese_survives(self) -> None:
        text = files.extract_text("整卷.pdf", self._pdf())
        self.assertIn("区间最大值", text)
        self.assertGreater(text.count("\n"), 3, "行结构丢了，切题会按错误行号切片")


@unittest.skipUnless(_has("docx"), "本机没有 python-docx")
class TestDocx(unittest.TestCase):
    def _docx(self) -> bytes:
        from docx import Document

        document = Document()
        for line in SAMPLE.splitlines():
            document.add_paragraph(line)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    def test_paragraphs(self) -> None:
        text = files.extract_text("整卷.docx", self._docx())
        self.assertIn("区间最大值", text)

    def test_tables_are_kept(self) -> None:
        """题面和样例常放在表格里，只读 paragraphs 会整段丢失。"""
        from docx import Document

        document = Document()
        document.add_paragraph(SAMPLE)
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "样例输入"
        table.rows[0].cells[1].text = "3\n1 2 3"
        buffer = io.BytesIO()
        document.save(buffer)
        text = files.extract_text("表格.docx", buffer.getvalue())
        self.assertIn("样例输入", text)
        self.assertIn("1 2 3", text)


class TestSupportedList(unittest.TestCase):
    def test_extensions_cover_common(self) -> None:
        for ext in (".pdf", ".docx", ".txt", ".md"):
            self.assertIn(ext, files.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
