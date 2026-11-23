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
        """认不出的二进制不再按后缀一口拒掉：先给转换器一次机会，失败才说话。"""
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("旧格式.doc", b"\x00\x01\x02" * 100)
        self.assertIn(ctx.exception.code, ("NEED_CONVERTER", "BINARY_FILE"))
        self.assertIn("另存为", str(ctx.exception))

    def test_truncated_pdf(self) -> None:
        """带 %PDF 头但内容损坏 → 明确说 PDF 打不开，不能当文本硬读。"""
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("a.pdf", b"%PDF-1.7\n" + b"\x00" * 200)
        self.assertIn(ctx.exception.code, ("BAD_PDF", "NO_PDF_ENGINE"))

    def test_text_renamed_to_pdf_is_read_as_text(self) -> None:
        """内容优先：一份 txt 被改成 .pdf，读成文本比报"PDF 损坏"有用。"""
        text = files.extract_text("其实是文本.pdf", ("题目一 区间最大值\n" * 10).encode())
        self.assertIn("区间最大值", text)


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

    def test_table_keeps_document_order(self) -> None:
        """表格在中间时不能挪到文末：样例跟着标题跑偏，后面就配不上对。"""
        from docx import Document

        document = Document()
        document.add_paragraph("题目一 区间最大值")
        document.add_paragraph("【题目描述】给定 n 个整数求最大值。")
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "输入"
        table.rows[0].cells[1].text = "输出"
        table.rows[1].cells[0].text = "3\n1 2 3"
        table.rows[1].cells[1].text = "3"
        document.add_paragraph("【输入格式】第一行一个整数 n。")
        buffer = io.BytesIO()
        document.save(buffer)

        text = files.extract_text("中间有表格.docx", buffer.getvalue())
        self.assertLess(
            text.index("【样例输入】"), text.index("【输入格式】"),
            "表格被挪到文末了，文档顺序丢失",
        )

    def test_sample_table_parses_into_samples(self) -> None:
        """表格版题面（首行 输入|输出）要能直接解析出样例组。"""
        from docx import Document

        from autocode.app.ingest_csp import parse_samples

        document = Document()
        document.add_paragraph("题目一 区间最大值")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "输入"
        table.rows[0].cells[1].text = "输出"
        for left, right in (("3\n1 2 3", "3"), ("1\n-5", "-5")):
            cells = table.add_row().cells
            cells[0].text = left
            cells[1].text = right
        buffer = io.BytesIO()
        document.save(buffer)

        text = files.extract_text("表格样例.docx", buffer.getvalue())
        samples, _external, warnings = parse_samples(text.split("\n"))
        self.assertEqual(len(samples), 2, f"表格样例没解析出来：{text}")
        self.assertEqual([item.output_text.strip() for item in samples], ["3", "-5"])
        self.assertEqual(warnings, [])

    def test_docx_renamed_to_doc_still_opens(self) -> None:
        """改名比另存为常见：容器按文件头判，不按后缀。"""
        text = files.extract_text("其实是docx的.doc", self._docx())
        self.assertIn("区间最大值", text)

    def test_old_doc_without_converter_says_how_to_fix(self) -> None:
        from unittest import mock

        with mock.patch.object(files, "_soffice_convert", return_value=None):
            with self.assertRaises(files.FileError) as ctx:
                files.extract_text("旧格式.doc", files._OLE2_MAGIC + b"\x00" * 100)
        self.assertEqual(ctx.exception.code, "NEED_CONVERTER")
        self.assertIn("另存为", str(ctx.exception))


class TestImages(unittest.TestCase):
    def test_is_image(self) -> None:
        for name in ("a.png", "b.JPG", "c.jpeg", "d.webp", "e.bmp", "f.gif"):
            self.assertTrue(files.is_image(name), name)
        for name in ("a.pdf", "a.docx", "a.txt"):
            self.assertFalse(files.is_image(name), name)

    def test_extract_text_refuses_images(self) -> None:
        """图片没有文字层：必须走模型转写，不能当二进制乱码读。"""
        with self.assertRaises(files.FileError) as ctx:
            files.extract_text("题.png", b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        self.assertEqual(ctx.exception.code, "IMAGE_NEEDS_VISION")


class TestSupportedList(unittest.TestCase):
    def test_extensions_cover_common(self) -> None:
        for ext in (".pdf", ".docx", ".doc", ".txt", ".md"):
            self.assertIn(ext, files.SUPPORTED)

    def test_images_are_supported(self) -> None:
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            self.assertIn(ext, files.SUPPORTED)


class TestSniffKind(unittest.TestCase):
    """分派只看内容：后缀会骗人，靠后缀补分支永远补不完。"""

    def test_image_without_extension(self) -> None:
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * 200
        self.assertEqual(files.sniff_kind("截图", raw), files.KIND_IMAGE)
        self.assertEqual(files.sniff_kind("截图.dat", raw), files.KIND_IMAGE)
        self.assertTrue(files.is_image("截图.dat", raw))

    def test_heic_is_image(self) -> None:
        raw = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
        self.assertEqual(files.sniff_kind("IMG_0001.HEIC", raw), files.KIND_IMAGE)

    def test_docx_by_container_not_suffix(self) -> None:
        import io as _io
        import zipfile as _zip

        buffer = _io.BytesIO()
        with _zip.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        self.assertEqual(files.sniff_kind("题目.doc", buffer.getvalue()), files.KIND_DOCX)

    def test_xlsx_goes_to_converter(self) -> None:
        import io as _io
        import zipfile as _zip

        buffer = _io.BytesIO()
        with _zip.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        self.assertEqual(files.sniff_kind("表.xlsx", buffer.getvalue()), files.KIND_OFFICE)

    def test_gb18030_text(self) -> None:
        raw = "题目一 区间最大值\n求最大值\n".encode("gb18030") * 5
        self.assertEqual(files.sniff_kind("题目.txt", raw), files.KIND_TEXT)


if __name__ == "__main__":
    unittest.main()
