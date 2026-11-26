"""上传文件的文本抽取：任何格式都从这里过一遍。

只做"把文件变成字符串"这一件事，切题仍走 `segment.segment_text`。
为什么不放在前端解析：PDF 的标准库在浏览器里要么体积巨大（pdf.js 约 1 MB），
要么抽出来的中文会缺字；服务器只需一个 pymupdf 就能稳定抽出整卷文字。

## 为什么按内容判类型，不按后缀

用户从手机、微信、WPS 里存出来的文件，后缀经常是错的（截图存成 `.dat`、
docx 改名成 `.doc`、PDF 其实是拍照导出的图片）。认后缀就要为每一种错法
补一条分支，永远补不完。这里只认**容器特征**（文件头），再由能力分派：

| 内容 | 走哪条 |
|---|---|
| `%PDF` | 抽文字层；没有文字层 → 渲染成图交给多模态模型 |
| zip 含 `word/` | python-docx 按文档顺序取段落与表格 |
| OLE2 / 其他 zip | LibreOffice 转 docx，转不动再转 PDF，再走上面两条 |
| 图片文件头 | `vision.transcribe`（多模态模型转写） |
| 无 NUL 字节 | 当文本解码（utf-8 / gb18030 / utf-16） |
| 都不匹配 | 仍试一次 LibreOffice，失败才给人话 |

失败一律给人话：缺组件说缺组件，加密 PDF 说加密了，不要让前端只看到 500。
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: base64 后体积会涨 1/3，服务端请求体上限 8 MB，这里留出余量。
MAX_BYTES = 5 * 1024 * 1024

#: 只用于界面上"支持哪些格式"的展示与旧调用方的后缀判断。
#: 真正的分派看 `sniff_kind`，不看这里。
IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif",
)

SUPPORTED = (
    ".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".csv", ".text",
    ".rtf", ".odt", ".html", ".htm", ".xlsx", ".pptx",
) + IMAGE_SUFFIXES

#: 内容类型（不是后缀）。解析器按这个分派，一个类型一条路径，不按题型不按后缀。
KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_OFFICE = "office"   # OLE2 旧格式、xlsx/pptx/odt：交给 LibreOffice
KIND_IMAGE = "image"
KIND_TEXT = "text"
KIND_UNKNOWN = "unknown"

_IMAGE_HEADERS = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
    b"BM", b"II*\x00", b"MM\x00*",
)

#: HEIC/HEIF/AVIF：`ftyp` 在第 5 字节起，品牌名决定是不是图片。
_FTYP_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif")

_TEXT_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")

#: 文件头嗅探：改后缀是常态（`.doc` 其实是 `.docx`、`.pdf` 其实是图片）。
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


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


def is_image(filename: str, raw: bytes = b"") -> bool:
    """是不是图片。给了字节就按文件头判（后缀经常是错的），否则退回后缀。"""
    if raw:
        return sniff_kind(filename, raw) == KIND_IMAGE
    return _suffix(filename) in IMAGE_SUFFIXES


def sniff_kind(filename: str, raw: bytes) -> str:
    """按**文件内容**判类型；后缀只在完全认不出时作参考。

    这一步是整个文件管线唯一的分派点：所有格式差异都在这里收敛成一个枚举，
    下游只按这个枚举走固定几条路径，不再出现"某某后缀要特殊处理"。
    """
    head = raw[:64]
    if head[:4] == b"%PDF":
        return KIND_PDF
    if head[:8] == _OLE2_MAGIC:
        return KIND_OFFICE
    if head[:4] == _ZIP_MAGIC:
        return _zip_kind(raw)
    if any(head.startswith(sig) for sig in _IMAGE_HEADERS):
        return KIND_IMAGE
    if head[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return KIND_IMAGE
    if head[4:8] == b"ftyp" and any(head[8:12].startswith(brand) for brand in _FTYP_BRANDS):
        return KIND_IMAGE
    if b"\x00" not in raw[:4096]:
        return KIND_TEXT
    # 还有一类：不规范的 PDF（前面带 BOM/空白）、被截断的 zip。给后缀最后一次机会。
    suffix = _suffix(filename)
    if suffix == ".pdf":
        return KIND_PDF
    if suffix in (".docx", ".doc", ".xlsx", ".pptx", ".odt", ".rtf"):
        return KIND_OFFICE
    return KIND_UNKNOWN


def _zip_kind(raw: bytes) -> str:
    """zip 家族：含 `word/` 才是 docx，其余（表格/PPT/ODT/EPUB）统一走转换器。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()[:400]
    except (zipfile.BadZipFile, OSError, ValueError):
        return KIND_OFFICE
    if any(name.startswith("word/") for name in names):
        return KIND_DOCX
    return KIND_OFFICE


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


#: 上标/下标字符表。缺字形的字符（大写字母、g/q/y/z 等）保守回退原样——
#: 宁可少转一个字符，也不能把 23 转成不存在的记号。
_SUP_CHARS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
    "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}
_SUB_CHARS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}
_SUP_TRANS = str.maketrans(_SUP_CHARS)
_SUB_TRANS = str.maketrans(_SUB_CHARS)


def _spanwise_text(spans: Sequence[Dict[str, Any]]) -> str:
    """一行 spans → 文本。比正文字号小一档且基线明显偏移的 span 转上/下标。

    判据是**几何**不是内容：CSP40 实测同一行里 `x²` 的 2（基线抬高 4.4pt）
    和 `aᵢ` 的 i（基线下沉 1.8pt）都是 8pt 小字，纯文本根本分不出来，
    span 的 origin.y 一眼定案。
    """
    sized = [sp for sp in spans if str(sp.get("text") or "").strip()]
    if not sized:
        return "".join(str(sp.get("text") or "") for sp in spans)
    base_size = max(float(sp.get("size") or 0) for sp in sized)
    base_y = 0.0
    for sp in sized:
        if abs(float(sp.get("size") or 0) - base_size) < 1e-6:
            origin = sp.get("origin") or (0.0, 0.0)
            base_y = float(origin[1])
            break
    parts: List[str] = []
    for sp in spans:
        text = str(sp.get("text") or "")
        if not text:
            continue
        try:
            size = float(sp.get("size") or 0)
            origin = sp.get("origin") or (0.0, 0.0)
            offset = float(origin[1]) - base_y
        except (TypeError, ValueError):
            parts.append(text)
            continue
        if (
            text.strip()
            and base_size > 0
            and size < base_size * 0.8
            and offset <= -1.0
        ):
            parts.append(text.translate(_SUP_TRANS))
        elif (
            text.strip()
            and base_size > 0
            and size < base_size * 0.8
            and offset >= 1.0
        ):
            parts.append(text.translate(_SUB_TRANS))
        else:
            parts.append(text)
    return "".join(parts)


def page_spanwise_lines(page: Any) -> List[Tuple[float, float, str]]:
    """单页逐行 `(y0, y1, 文本)`，行序与 `get_text("dict")` 一致。

    文本经 `_spanwise_text` 处理：上/下标在抽取时就位。调用方要用它同时产出
    行文本和行坐标（`extract_pdf_bundle`），两边同源才不会像 get_text()/dict
    那样需要对齐校验。
    """
    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001 - 拿不到 dict 就退化成无转换
        return []
    out: List[Tuple[float, float, str]] = []
    for block in data.get("blocks", []):
        if int(block.get("type", -1)) != 0:
            continue
        for line in block.get("lines", []):
            spans = [sp for sp in line.get("spans", []) if str(sp.get("text") or "")]
            if not spans:
                continue
            text = _spanwise_text(spans)
            if not text.strip():
                continue
            bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
            out.append((float(bbox[1]), float(bbox[3]), text))
    return out


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
            span_pages: List[str] = []
            for page in doc:
                lines = page_spanwise_lines(page)
                span_pages.append("\n".join(text for _, _, text in lines))
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
    # span 感知版抽得出字就优先用：上标/下标在抽取时就转成对应字符，
    # `x2 + k2 mod 23` 这类丢失指数的记号不必等导入规则去猜
    text = "\n".join(span_pages).strip() or "\n".join(pages).strip()
    if len(text) < 50:
        raise FileError(
            "这份 PDF 抽不出文字，可能是扫描件（图片）。"
            "请改用「图片」方式上传（需多模态模型），或先换成带文字层的 PDF",
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
    parts = _docx_lines(document)
    text = "\n".join(parts).strip()
    if len(text) < 50:
        raise FileError("这份 Word 文档里几乎没有文字", "EMPTY_DOCX")
    return text


def _docx_lines(document: Any) -> List[str]:
    """按**文档顺序**取段落与表格。

    不能先取 `document.paragraphs` 再追加 `document.tables`：题面里的样例表
    （输入/输出两列表）会被整体挪到文末，样例就不在题面中间了，解析出来对不上。
    文本框里的字挂在 `w:txbxContent` 下，`paragraph.text` 取不到，所以按 `w:t`
    逐段拼：段内顺序就是视觉顺序。
    """
    from docx.oxml.ns import qn  # type: ignore
    from docx.table import Table  # type: ignore

    parts: List[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            parts.append("".join(node.text or "" for node in child.iter(qn("w:t"))))
        elif child.tag == qn("w:tbl"):
            parts.extend(_table_lines(Table(child, document)))
    return parts


#: 表格版题面：首行两列分别是"输入 / 输出"时，按样例表处理。
_SAMPLE_COL_INPUT = {"输入", "样例输入", "输入样例", "输入数据", "input"}
_SAMPLE_COL_OUTPUT = {"输出", "样例输出", "输出样例", "输出数据", "output"}


def _cell_key(text: str) -> str:
    return re.sub(r"[\s【】\[\]（()：:]", "", str(text or "")).lower()


def _table_lines(table: Any) -> List[str]:
    """表格 → 文本行。

    首行是「输入 | 输出」两列的表格几乎都是样例表：按列拆成
    `【样例输入】…【样例输出】…`，这样后面的样例解析认得出配对关系。
    其余表格按行用制表符拼——保留"哪几格在同一行"的信息，不做额外猜测。
    """
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    rows = [row for row in rows if any(row)]
    if len(rows) >= 2 and len(rows[0]) == 2:
        left, right = _cell_key(rows[0][0]), _cell_key(rows[0][1])
        if left in _SAMPLE_COL_INPUT and right in _SAMPLE_COL_OUTPUT:
            parts: List[str] = []
            for row in rows[1:]:
                if len(row) < 2:
                    continue
                parts.append("【样例输入】")
                parts.extend(str(row[0]).split("\n"))
                parts.append("【样例输出】")
                parts.extend(str(row[1]).split("\n"))
            return parts
    return ["\t".join(row) for row in rows]


def _soffice_convert(raw: bytes, target: str, source_suffix: str = ".doc") -> Optional[bytes]:
    """用 LibreOffice 无头模式转成 `target`（docx / pdf）；没有 soffice 或失败返回 None。

    只在装了 LibreOffice 的机器上生效（Linux 服务器常见，Windows 默认不在 PATH）。
    转换放入临时目录，失败不影响原文件。
    """
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="autocode-doc-") as workdir:
        stem = "input"
        source = Path(workdir) / (stem + (source_suffix or ".bin"))
        source.write_bytes(raw)
        try:
            subprocess.run(
                [binary, "--headless", "--norestore", "--convert-to", target,
                 "--outdir", workdir, str(source)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90, check=False,
                env={**os.environ, "HOME": workdir},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        produced = Path(workdir) / f"{stem}.{target.split(':')[0]}"
        return produced.read_bytes() if produced.is_file() else None


def _from_office(raw: bytes, suffix: str = "") -> str:
    """Office 家族：先转 docx（结构最完整），转不动再转 PDF。

    这一步不看具体是 .doc / .xls / .ppt / .odt：它们对 LibreOffice 是同一类输入，
    对我们就只有"能不能读出字"一个问题。
    """
    source = suffix if suffix in (".doc", ".xls", ".ppt", ".rtf", ".odt") else ".doc"
    converted = _soffice_convert(raw, "docx", source)
    if converted is not None:
        return _from_docx(converted)
    as_pdf = _soffice_convert(raw, "pdf", source)
    if as_pdf is not None:
        return _from_pdf(as_pdf)
    raise FileError(
        "这个文件是 Office 二进制格式，服务器上没有可用的转换器。"
        "请用 Office / WPS 打开后「另存为 .docx」或导出 PDF 再上传",
        "NEED_CONVERTER",
    )


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

            # 文本与坐标同源：都从 get_text("dict") 的 span 里来。
            # 旧写法 get_text() 一份、dict 一份，两边对空行/行序的取舍不一致，
            # 靠 60% 相似度对齐；同源后天生精确一致，且文本带上/下标转换。
            span_lines = [page_spanwise_lines(page) for page in doc]
            page_texts = ["\n".join(text for _, _, text in lines) for lines in span_lines]
            line_boxes = span_lines
            blocks, warnings = enum_figures(doc)

            spans: Dict[int, List[Tuple[float, float]]] = {}
            for block in blocks:
                spans.setdefault(int(block.page), []).append(
                    (float(block.bbox[1]), float(block.bbox[3]))
                )

            # 每页先做两件事：删页眉页脚、删"画在图里的文字"。
            # 后者必须做：CSP32 的算法过程图里全是 `1, 10`、`No` 这类标签，
            # 留在题面里会把一句话劈成两半（实测"此时应当去｜…图3…｜掉类别5"）。
            lines_by_page: List[List[str]] = []
            coords_by_page: List[List[Optional[Tuple[float, float]]]] = []
            for index, text in enumerate(page_texts):
                lines, coords = _page_layout(
                    text.split("\n"), line_boxes[index], spans.get(index + 1, [])
                )
                lines_by_page.append(lines)
                coords_by_page.append(coords)

            texts = ["\n".join(lines) for lines in lines_by_page]
            # 行号必须按这份**已清洗**的文本算：切题那边拿到的是同一份，
            # 两份坐标差几十行时图会按页码挂到隔壁题上（实测 CSP32 复现）
            ranges = line_ranges(texts)
            figures = []
            for block in blocks:
                record = block.to_dict()
                # 图该插在哪一行之后：只按页码会把图放到页首，
                # 实测 CSP32 树图画在下半页，按页码算成了题面第 1 行
                record["anchor_line"] = _anchor_line(block, coords_by_page, ranges)
                figures.append(record)
    except Exception:  # noqa: BLE001 - 增强功能失败不该挡住导入
        return None
    return {
        # 不 strip：行号就是按这份文本数的，去掉首部空行会让所有锚点错位
        "text": "\n".join(texts),
        "figures": figures,
        "page_ranges": [[int(start), int(end)] for start, end in ranges],
        "warnings": warnings,
    }


def _squeeze(text: str) -> str:
    """去掉全部空白，只留可比较的字符：抽行与 dict 抽行对空格的取舍不一致。"""
    return "".join(str(text or "").split())


def _inside_figure(span: Tuple[float, float], figures: Sequence[Tuple[float, float]]) -> bool:
    """这行文字是否整个落在某个图块内（按纵向判断：图内标签几乎都独占一行）。"""
    top, bottom = span
    for fig_top, fig_bottom in figures:
        if top >= fig_top - 2.0 and bottom <= fig_bottom + 2.0:
            return True
    return False


def _page_layout(
    raw_lines: Sequence[str],
    boxes: Sequence[tuple],
    figures: Sequence[Tuple[float, float]],
) -> Tuple[List[str], List[Optional[Tuple[float, float]]]]:
    """一页的逐行文本与纵坐标：删页眉页脚、删图内文字，空行原样保留。

    坐标来自 `get_text("dict")`（只含有字的行），文本来自 `get_text()`（跳过空行），
    两边靠"非空行数相同 + 六成文本对得上"确认对齐；对不上就不删任何图内行——
    宁可留点噪声，也不能把正文误删。
    """
    from .segment import is_page_furniture  # 局部导入：避免 files ↔ segment 循环依赖

    filled = [index for index, line in enumerate(raw_lines) if line.strip()]
    aligned = len(filled) == len(boxes)
    if aligned:
        same = sum(
            1 for index, box in zip(filled, boxes) if _squeeze(raw_lines[index]) == _squeeze(box[2])
        )
        aligned = same >= max(1, int(len(boxes) * 0.6))
    coords = {index: (float(box[0]), float(box[1])) for index, box in zip(filled, boxes)} if aligned else {}

    lines: List[str] = []
    out_coords: List[Optional[Tuple[float, float]]] = []
    for index, line in enumerate(raw_lines):
        if is_page_furniture(line):
            continue
        span = coords.get(index)
        if span is not None and figures and _inside_figure(span, figures):
            continue
        lines.append(line)
        out_coords.append(span)
    return lines, out_coords


def _anchor_line(
    block: Any,
    coords_by_page: Sequence[Sequence[Optional[Tuple[float, float]]]],
    ranges: Sequence[Sequence[int]],
) -> int:
    """图块「该插在哪一行之后」的整卷行号；判断不出来返回 0（调用方退回页首）。

    取图的上边之上、最靠下的一行文字：只按页码会把图一律放到页首，
    实测 CSP32 第 6 页的树图画在下半页，按页码得到的是题面第 1 行。
    """
    index = int(block.page) - 1
    if not (0 <= index < len(coords_by_page)):
        return 0
    top = float(block.bbox[1])
    anchor = 0
    for offset, span in enumerate(coords_by_page[index]):
        if span is None:
            continue
        if span[1] <= top + 2.0:  # 2pt 容差：图和上一行常贴着
            anchor = int(ranges[index][0]) + offset
        else:
            break
    return anchor


def _from_word(raw: bytes, suffix: str = "") -> str:
    """Word 容器按文件头判，不按后缀：改名比另存为常见。"""
    if raw[:4] == _ZIP_MAGIC:
        return _from_docx(raw)
    if raw[:8] == _OLE2_MAGIC:
        return _from_office(raw, suffix)
    raise FileError(
        "这看起来是二进制文件（.doc/.xls 等旧格式）或已损坏，"
        "请用 Word / WPS 另存为 .docx 再传，或先导出成 PDF",
        "BINARY_FILE",
    )


def render_pdf_pages(raw: bytes, limit: int = 4, dpi: int = 150) -> List[bytes]:
    """把 PDF 每页渲染成 PNG：扫描件没有文字层时，交给多模态模型"看"。

    这是"任何文件都能进"的最后一环：PDF 抽不出字不等于读不了，
    只是要走图片那条路——不应该让用户自己去截图再传一次。
    """
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise FileError("服务器缺少 PDF 渲染组件（pymupdf），请联系管理员安装", "NO_PDF_ENGINE") from exc
    pages: List[bytes] = []
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            if doc.needs_pass:
                raise FileError("这份 PDF 有打开密码，请先解除密码再上传", "ENCRYPTED_PDF")
            for index, page in enumerate(doc):
                if index >= limit:
                    break
                pages.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    except FileError:
        raise
    except Exception as exc:  # noqa: BLE001 - pymupdf 异常类型很杂
        raise FileError("这份 PDF 打不开：文件可能损坏或被截断", "BAD_PDF") from exc
    if not pages:
        raise FileError("这份 PDF 里没有页面", "BAD_PDF")
    return pages


def extract_text(filename: str, raw: bytes) -> str:
    """把任意文件变成纯文本：按内容选解析器，不按后缀。"""
    kind = sniff_kind(filename, raw)
    suffix = _suffix(filename)
    if kind == KIND_IMAGE:
        raise FileError(
            "图片要先由模型识别成文字，请从导入页上传（需要配置支持图片的模型）",
            "IMAGE_NEEDS_VISION",
        )
    if kind == KIND_PDF:
        text = _from_pdf(raw)
    elif kind == KIND_DOCX:
        text = _from_docx(raw)
    elif kind == KIND_OFFICE:
        text = _from_office(raw, suffix)
    elif kind == KIND_TEXT:
        text = _from_plain(raw)
    else:
        # 认不出的二进制：仍给转换器一次机会（很多专业格式它都能读），
        # 失败才说"读不了"——比按后缀一口气拒掉多一次机会。
        text = _from_office(raw, suffix)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text.strip()) < 50:
        raise FileError("文件里可用的文字太少（不足 50 字），请检查内容", "TOO_SHORT")
    return text
