"""上传文件的文本抽取：PDF / DOCX / 纯文本。

只做"把文件变成字符串"这一件事，切题仍走 `segment.segment_text`。
为什么不放在前端解析：PDF 的标准库在浏览器里要么体积巨大（pdf.js 约 1 MB），
要么抽出来的中文会缺字；服务器只需一个 pymupdf 就能稳定抽出整卷文字。

失败一律给人话：缺组件说缺组件，加密 PDF 说加密了，不要让前端只看到 500。
"""

from __future__ import annotations

import base64
import io
from typing import Any, Dict, List, Optional, Tuple

#: base64 后体积会涨 1/3，服务端请求体上限 8 MB，这里留出余量。
MAX_BYTES = 5 * 1024 * 1024

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".text")

_TEXT_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")


class FileError(RuntimeError):
    """抽取失败：`code` 供 HTTP 层映射状态码。"""

    def __init__(self, message: str, code: str = "BAD_FILE") -> None:
        super().__init__(message)
        self.code = code


def _suffix(name: str) -> str:
    lowered = str(name or "").strip().lower()
    for ext in SUPPORTED:
        if lowered.endswith(ext):
            return ext
    return ""


def decode_upload(payload: dict) -> Tuple[str, bytes]:
    """从 `{filename, content_b64}` 取出 `(文件名, 原始字节)`，顺手做体积校验。"""
    name = str(payload.get("filename") or "").strip()[:160]
    raw_b64 = str(payload.get("content_b64") or "").strip()
    if not raw_b64:
        raise FileError("没有收到文件内容", "INVALID_INPUT")
    try:
        raw = base64.b64decode(raw_b64, validate=False)
    except (ValueError, TypeError) as exc:
        raise FileError("文件内容不是合法的 base64", "INVALID_INPUT") from exc
    if not raw:
        raise FileError("文件是空的", "INVALID_INPUT")
    if len(raw) > MAX_BYTES:
        raise FileError(f"文件超过 {MAX_BYTES // (1024 * 1024)} MB，请拆分后再传", "TOO_LARGE")
    return name or "上传文件", raw


def _from_pdf(raw: bytes) -> str:
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise FileError("服务器缺少 PDF 解析组件（pymupdf），请联系管理员安装", "NO_PDF_ENGINE") from exc
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            if doc.needs_pass:
                raise FileError("这份 PDF 有打开密码，请先解除密码再上传", "ENCRYPTED_PDF")
            pages = [page.get_text() for page in doc]
    except FileError:
        raise
    except Exception as exc:  # noqa: BLE001 - pymupdf 的异常类型很杂
        # 异常类名（FileDataError 之类）对使用者毫无意义，只说人话；
        # 原始异常留在 __cause__ 里，排查时看服务端 traceback 即可
        raise FileError(
            "这份 PDF 打不开：文件可能损坏、被截断，或并不是真正的 PDF。"
            "请先用阅读器确认能正常打开，再重新上传",
            "BAD_PDF",
        ) from exc
    text = "\n".join(pages).strip()
    if len(text) < 50:
        raise FileError(
            "这份 PDF 抽不出文字，可能是扫描件（图片）。请用带文字层的 PDF，或先做 OCR",
            "EMPTY_PDF",
        )
    return text


def _from_docx(raw: bytes) -> str:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:
        raise FileError("服务器缺少 Word 解析组件（python-docx），请联系管理员安装", "NO_DOCX_ENGINE") from exc
    try:
        document = Document(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise FileError(
            "这份 Word 打不开：可能是旧版 .doc 改了后缀，或文件已损坏。"
            "请另存为 .docx 或 PDF 后再上传",
            "BAD_DOCX",
        ) from exc
    parts = [para.text for para in document.paragraphs]
    # 表格里的题面也不算少见（题目和样例分栏放），按行拼回去
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    text = "\n".join(parts).strip()
    if len(text) < 50:
        raise FileError("这份 Word 文档里几乎没有文字", "EMPTY_DOCX")
    return text


def _from_plain(raw: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_pdf_bundle(raw: bytes) -> Optional[Dict[str, Any]]:
    """PDF 额外产出图块清单与页-行映射；非 PDF 或解析失败返回 None。

    这些信息只用于"把图分给正确的题"：图块带页码，页-行映射把页码翻成行号，
    切题后按"题目行号区间 ∩ 图所在页行号区间"做几何归属，不额外调模型。

    解析失败一律返回 None 而不是抛错：图是增强功能，不能因为它让整个导入失败。
    """
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return None
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            if doc.needs_pass:
                return None
            from ..app.pdf_figures import enum_figures, line_ranges
            from .segment import strip_page_furniture

            page_texts = [page.get_text() for page in doc]
            # 页-行映射必须按**清洗后**的文本算：切题那边会先删页眉页脚再编号，
            # 两份坐标差 60 多行，图就会按页码挂到隔壁题上（实测 CSP32 复现）。
            cleaned_texts = [
                "\n".join(strip_page_furniture(text.split("\n"))[0]) for text in page_texts
            ]
            blocks, warnings = enum_figures(doc)
            ranges = line_ranges(cleaned_texts)
    except Exception:  # noqa: BLE001 - 增强功能失败不该挡住导入
        return None
    return {
        "text": "\n".join(page_texts).strip(),
        "figures": [block.to_dict() for block in blocks],
        "page_ranges": [[int(start), int(end)] for start, end in ranges],
        "warnings": warnings,
    }


def extract_text(filename: str, raw: bytes) -> str:
    """按扩展名抽取纯文本；不认识的后缀按纯文本处理（多数是 .txt 改名）。"""
    suffix = _suffix(filename)
    if suffix == ".pdf":
        text = _from_pdf(raw)
    elif suffix == ".docx":
        text = _from_docx(raw)
    else:
        text = _from_plain(raw)
        if "\x00" in text[:2000]:
            raise FileError("这看起来是二进制文件（.doc/.xls 等旧格式），请另存为 .docx 或 .pdf 再传", "BINARY_FILE")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text.strip()) < 50:
        raise FileError("文件里可用的文字太少（不足 50 字），请检查内容", "TOO_SHORT")
    return text
