"""整卷 PDF → 多模态切题 → 原文切片 + 插图入库（B 方案，用户 2026-11-17 定案）。

职责边界（关键，v2 仍然不变）：
- LLM **只决定边界**（title / slug / start_line / end_line），不输出题面正文，更不输出样例；
- 题面与样例由代码按行号从**原始文本**逐字切片，模型写的正文一律不采用；
- 每题至少 1 组完整样例，否则不入库并列出原因（宁缺勿错）。

v2（2026-11-21）：模型能看图了，但**坐标仍由代码算**。
- 代码用 pymupdf 枚举全部图块并编号（`fig_1`、`fig_2`…），把页图发给模型；
- 模型只回答"哪几个编号属于这道题"和"图该插在第几行之后"；
- 裁剪按代码算出的 bbox 执行，图片是原始像素，不经模型转述。

为什么不让模型直接给 bbox：视觉模型返回的坐标是估算值，页图缩放到约 1300px 后
误差可达几十 pt，裁出来边角必缺。语义判断归模型，几何精度归代码。
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..prompts import loader as prompts
from .ingest_csp import (
    IngestReport,
    ProblemSection,
    ingest_sections,
    parse_samples,
)
from .pdf_figures import (
    FigureBlock,
    enum_figures,
    export_figures,
    line_ranges,
    render_page_jpeg,
)

#: PDF 导入与 md 导入共用 `CSP` 这个 source：两者是同一套题库，
#: 用不同 source 会让 `(source, source_id)` 去重失效，同一道题被重复录入。
#: （实测：CSP37 从 md 导入是 `CSP/CSP37-integral`，PDF 再导一次会变成
#: `CSP-PDF/CSP37-integral`，库里出现两道「数值积分」。）
SOURCE_NAME = "CSP"
#: 单次送给模型的行数上限（16 页试卷约 500~800 行；超过则截断并明确告警）。
MAX_SEGMENT_LINES = 4000
#: 一题至少要有这么多行，避免模型切出只有标题/页眉的空段。
MIN_SEGMENT_LINES = 4
#: 单次调用最多送多少张页图。超过后只送文本（图片太多会顶到请求体积上限）。
MAX_PAGE_IMAGES = 24
#: 图片资产根目录（相对项目根，前端按 `/assets/<stem>/...` 访问）。
ASSETS_ROOT = Path(__file__).resolve().parents[2] / "data" / "assets"

_TITLE_SUFFIX_RE = re.compile(r"（([a-z0-9][a-z0-9_-]*)）\s*$")
_CERT_PREFIX_RE = re.compile(r"^第\s*\d+\s*次认证")
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")
#: 每页重复的页眉页脚。CSP 试卷的 PDF 文本层常把它们拼成一行
#: （`第13 页共15 页CCF CSP 计算机软件能力认证 第37 次认证收费标准评估（cspark）`），
#: 所以要按"行首特征"匹配，不能要求整行相等。
_PAGE_FURNITURE_RE = re.compile(
    r"^\s*(?:第\s*\d+\s*页|共\s*\d+\s*页|CCF\s*CSP|第\s*\d+\s*次认证)"
)


def _drop_page_furniture(text: str) -> str:
    """剔除页眉页脚行。

    必须在**整卷**层面做，且喂给模型与喂给切片的都是同一份清洗后文本，
    这样行号一致、`after_line` 不会错位。只删页眉行，不做 Unicode 归一
    （题面里的 `−5` 要原样保留，那是题目内容）。
    """
    kept = [line for line in str(text or "").splitlines() if not _PAGE_FURNITURE_RE.match(line)]
    return "\n".join(kept)


@dataclass
class Segment:
    """LLM 给出的题目边界（行号 1-based，闭区间）。"""

    title: str
    slug: str
    start_line: int
    end_line: int
    after_line: int = 0
    figure_ids: List[str] = field(default_factory=list)
    figure_missing: bool = False
    #: 每张图各自的插入位置 `{fig_id: 行号}`；缺省时退回 `after_line`
    figure_positions: Dict[str, int] = field(default_factory=dict)


@dataclass
class PdfIngestResult:
    report: IngestReport
    segments: List[Segment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    figures_total: int = 0
    page_images: int = 0

    def to_dict(self) -> Dict[str, Any]:
        # 去重：切题告警既进 result.warnings 又进 report.warnings（ingest 报告统一展示），
        # 直接拼接会让同一条在界面上出现两次。
        warnings = list(dict.fromkeys([*self.warnings, *self.report.warnings]))
        return {
            "path": self.report.path,
            "segments": [dict(seg.__dict__) for seg in self.segments],
            "imported": [
                {
                    "id": item.problem_id,
                    "title": item.title,
                    "source_id": item.source_id,
                    "samples": len(item.samples),
                    "figures": len(item.figures),
                    "created": item.created,
                }
                for item in self.report.problems
            ],
            "skipped": self.skipped,
            "figures_total": self.figures_total,
            "page_images": self.page_images,
            "warnings": warnings,
        }


def _pymupdf():
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:  # pragma: no cover - 老版本包名
        import fitz as pymupdf  # type: ignore

        return pymupdf


def extract_pdf_text(path: Path | str) -> str:
    """抽取 PDF 纯文本。pymupdf 是硬依赖（见 requirements.txt）。"""
    pymupdf = _pymupdf()
    with pymupdf.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def _numbered(lines: Sequence[str]) -> str:
    width = len(str(len(lines)))
    return "\n".join(f"{index:0{width}d}| {line}" for index, line in enumerate(lines, start=1))


def _clean_title(raw: str) -> str:
    title = _CERT_PREFIX_RE.sub("", str(raw or "").strip())
    title = _TITLE_SUFFIX_RE.sub("", title).strip()
    return title[:40]


def _clean_slug(raw: str, ordinal: int) -> str:
    slug = _SLUG_CLEAN_RE.sub("", str(raw or "").strip().lower())
    return slug or f"p{ordinal}"


def _snap_start(lines: Sequence[str], segment: Segment) -> Segment:
    """LLM 常把页眉「第32次认证仓库规划（plan）」当成标题行，向后吸附到正式标题。"""
    index = segment.start_line - 1
    if not (0 <= index < len(lines)):
        return segment
    head = lines[index]
    looks_like_header = (
        "次认证" in head or head.strip().startswith("CCF CSP")
        or f"（{segment.slug}）" not in head
    )
    if not looks_like_header:
        return segment
    for offset in range(1, 9):
        probe = index + offset
        if probe >= len(lines):
            break
        if f"（{segment.slug}）" in lines[probe] and "次认证" not in lines[probe]:
            return Segment(
                segment.title,
                segment.slug,
                probe + 1,
                max(segment.end_line, probe + 2),
                segment.after_line,
                list(segment.figure_ids),
                segment.figure_missing,
                dict(segment.figure_positions),
            )
    return segment


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_segments(
    payload: Dict[str, Any],
    total_lines: int,
    *,
    valid_figures: Optional[Sequence[str]] = None,
    figure_pages: Optional[Dict[str, int]] = None,
    page_ranges: Optional[Sequence[Tuple[int, int]]] = None,
) -> Tuple[List[Segment], List[str]]:
    """校验并归一 LLM 返回的边界：越界/倒序/重叠一律丢弃并说明。

    `valid_figures` 是代码枚举出的图块编号白名单；模型编造不存在的编号会被剔除。
    `figure_pages` + `page_ranges` 给图块归属加一道几何校验：第 P 页的图只能分给
    行号区间覆盖第 P 页的题（实测模型对跨章图会给三种不同答案，靠这个收敛）。
    """
    warnings: List[str] = []
    items = payload.get("problems") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], ["模型未返回 problems 数组"]
    allowed = set(valid_figures or [])
    candidates: List[Segment] = []
    for ordinal, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = _clean_title(item.get("title") or "")
        slug = _clean_slug(item.get("slug") or "", ordinal)
        start = _as_int(item.get("start_line"))
        end = _as_int(item.get("end_line"))
        if start is None or end is None:
            warnings.append(f"{title or f'第{ordinal}题'}：行号不是整数，已丢弃")
            continue
        if not (1 <= start <= end <= total_lines):
            warnings.append(f"{title or slug}：行号 {start}~{end} 越界（1~{total_lines}），已丢弃")
            continue
        if end - start + 1 < MIN_SEGMENT_LINES:
            warnings.append(f"{title or slug}：区间只有 {end - start + 1} 行，疑似误切，已丢弃")
            continue

        # 新格式 `figures: [{"id":"fig_3","after_line":1180}]` 优先；
        # 旧格式 `figure_ids: ["fig_3"]` 继续接受（只加不改）。
        figure_ids: List[str] = []
        figure_positions: Dict[str, int] = {}
        entries: List[Tuple[Any, Any]] = []
        raw_figures = item.get("figures")
        if isinstance(raw_figures, list):
            for raw in raw_figures:
                if isinstance(raw, dict):
                    entries.append((raw.get("id"), raw.get("after_line")))
                else:
                    entries.append((raw, None))
        raw_ids = item.get("figure_ids")
        if isinstance(raw_ids, list):
            for raw_id in raw_ids:
                entries.append((raw_id, None))
        for raw_id, raw_after in entries:
            figure_id = str(raw_id or "").strip()
            if not figure_id:
                continue
            if allowed and figure_id not in allowed:
                warnings.append(f"{title or slug}：图块 {figure_id} 不存在，已剔除")
                continue
            if figure_pages and page_ranges:
                page = int(figure_pages.get(figure_id) or 0)
                if 1 <= page <= len(page_ranges):
                    page_start, page_end = page_ranges[page - 1]
                    if page_end < start or page_start > end:
                        warnings.append(
                            f"{title or slug}：图块 {figure_id} 在第{page}页"
                            f"（行{page_start}-{page_end}），不在本题行号 {start}-{end} 内，已剔除"
                        )
                        continue
            if figure_id not in figure_ids:
                figure_ids.append(figure_id)
            position = _as_int(raw_after) or 0
            if position and start <= position <= end:
                figure_positions[figure_id] = position

        after_line = _as_int(item.get("after_line")) or 0
        if after_line and not (start <= after_line <= end):
            after_line = 0  # 落在区间外说明模型记错了，退回"插在题首"
        candidates.append(
            Segment(
                title or slug,
                slug,
                start,
                end,
                after_line,
                figure_ids,
                bool(item.get("figure_missing")),
                figure_positions,
            )
        )

    candidates.sort(key=lambda seg: seg.start_line)
    kept: List[Segment] = []
    seen_slug = set()
    for segment in candidates:
        if segment.slug in seen_slug:
            warnings.append(f"{segment.title}：slug={segment.slug} 重复，已丢弃后一条")
            continue
        if kept and segment.start_line <= kept[-1].end_line:
            previous = kept[-1]
            if (
                segment.start_line == previous.end_line
                and previous.end_line - 1 > previous.start_line
            ):
                # 模型把下一题标题行算进了上一题（常见，尤其标题在页首时）。
                # 这一行属于下一题，把上一题收一格，不要因此丢掉整题。
                warnings.append(
                    f"{previous.title}：与下一题共用第 {segment.start_line} 行，"
                    "已把该行让给下一题"
                )
                previous.end_line -= 1
            else:
                warnings.append(
                    f"{segment.title}：与上一题区间重叠"
                    f"（{segment.start_line} <= {previous.end_line}），已丢弃"
                )
                continue
        seen_slug.add(segment.slug)
        kept.append(segment)
    return kept, warnings


def _fallback_segments(lines: Sequence[str]) -> List[Segment]:
    """模型切不出时的确定性兜底：只在“裸 PDF 标题行”上工作，样例仍由代码抽。"""
    heads: List[Tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if "次认证" in text or text.startswith("CCF CSP"):
            continue
        match = _TITLE_SUFFIX_RE.search(text)
        if match and 1 <= len(text) <= 40:
            heads.append((index, _clean_title(text), _clean_slug(match.group(1), len(heads) + 1)))
    segments: List[Segment] = []
    for ordinal, (index, title, slug) in enumerate(heads):
        end = heads[ordinal + 1][0] - 1 if ordinal + 1 < len(heads) else len(lines)
        if end - index + 1 >= MIN_SEGMENT_LINES:
            segments.append(Segment(title, slug, index + 1, end + 1))
    return segments


def _figure_catalog(blocks: Sequence[FigureBlock]) -> str:
    """拼图块清单：模型只能从这些编号里选，坐标不经过模型。"""
    if not blocks:
        return "（本卷未检测到任何图块）"
    return "\n".join(
        f"{block.id} | 第{block.page}页 | {block.width:.0f}x{block.height:.0f}pt | {block.kind}"
        for block in blocks
    )


def _user_text(lines: Sequence[str], blocks: Sequence[FigureBlock]) -> str:
    head = (
        f"总行数：{len(lines)}\n\n"
        "# 图块清单（程序枚举，坐标已精确，无需你写坐标）\n\n"
        f"{_figure_catalog(blocks)}\n\n"
        "# 整卷文本（每行 `行号| 原文`）\n\n"
    )
    return head + _numbered(lines)


def _source_prefix(stem: str, text: str) -> str:
    """题目的 source_id 前缀：优先取文件名里的 CSP 编号，其次取正文里的「第 N 次认证」。

    为什么不能用文件名：`CSP37-statement.pdf` 会得到 `CSP37-statement-integral`，
    而 md 路径导库时用的是 `CSP37-integral` —— 同一道题被当成两道，重导一次就多 5 道重复题。
    """
    match = re.search(r"(CSP\s*\d+)", stem, flags=re.IGNORECASE)
    if match:
        return re.sub(r"\s+", "", match.group(1)).upper()
    match = re.search(r"第\s*(\d+)\s*次认证", text)
    if match:
        return f"CSP{match.group(1)}"
    return stem


def segment_with_llm(
    client: Any,
    text: str,
    *,
    tag: str = "segment_pdf#1",
    page_images: Optional[Sequence[Tuple[int, bytes]]] = None,
    blocks: Optional[Sequence[FigureBlock]] = None,
    page_ranges: Optional[Sequence[Tuple[int, int]]] = None,
) -> Tuple[List[Segment], List[str]]:
    """请模型标边界与图块归属；返回 (segments, warnings)。不做任何入库。

    `page_images` 为 `[(页码, JPEG字节)]`；为空时退回纯文本调用（旧行为，可随时降级）。
    """
    lines = text.splitlines()
    warnings: List[str] = []
    if len(lines) > MAX_SEGMENT_LINES:
        warnings.append(
            f"文本 {len(lines)} 行超过上限 {MAX_SEGMENT_LINES}，仅送前 {MAX_SEGMENT_LINES} 行；"
            "后面的题目不会被切出"
        )
        lines = lines[:MAX_SEGMENT_LINES]
    system_text = prompts.load(prompts.FILE_SEGMENT)
    body_text = _user_text(lines, blocks or [])

    content: Any = body_text
    if page_images:
        content = [{"type": "text", "text": body_text}]
        for page_no, jpeg in page_images:
            encoded = base64.b64encode(jpeg).decode("ascii")
            content.append({"type": "text", "text": f"【第 {page_no} 页】"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )

    result = client.chat(
        [
            {"role": "system", "content": system_text},
            {"role": "user", "content": content},
        ],
        json_mode=True,
        # 切题是"照着标题行和页图做结构化标注"，不需要推理链。
        # 实测：开 thinking 时预算全烧在 reasoning 上（4096 打满、8192 也打满），
        # 每次都要靠 client 关思考重试才出结果；直接关掉能一次过，还省一半 token。
        thinking="disabled",
        max_tokens=4096,
        temperature=0.0,
        tag=tag,
        stage="segment",
        problem_id=None,
        prompt_hash=hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16],
    )
    if page_images and not getattr(client, "_vision_supported", True):
        warnings.append("当前模型不接受图片输入，本轮已降级为纯文本切题（图片归属需人工核对）")
    segments, problems = parse_segments(
        result.json_obj or {},
        len(lines),
        valid_figures=[block.id for block in (blocks or [])],
        figure_pages={block.id: block.page for block in (blocks or [])},
        page_ranges=page_ranges,
    )
    warnings.extend(problems)
    if not segments:
        fallback = _fallback_segments(lines)
        if fallback:
            warnings.append(f"模型未给出可用边界，已用标题行兜底切出 {len(fallback)} 题")
            segments = fallback
    return segments, warnings


def _trim_trailing_artifacts(block: List[str]) -> List[str]:
    """去掉切片尾部属于“下一页页眉/页码/下一题标题”的行。

    LLM 的 end_line 是“下一题标题前一行”，而 PDF 的页眉常出现在下一题标题之前，
    不清理就会把 `CCF CSP … 第32次认证宝藏（treasure）` 粘在上一题末尾。
    """
    page_head = re.compile(r"^第\s*\d+\s*页")
    page_foot = re.compile(r"^共\s*\d+\s*页")
    while block:
        tail = block[-1].strip()
        if (
            not tail
            or tail.startswith("CCF CSP")
            or "次认证" in tail
            or page_head.match(tail)
            or page_foot.match(tail)
            or (len(tail) <= 40 and _TITLE_SUFFIX_RE.search(tail))
        ):
            block.pop()
        else:
            break
    return block


def build_sections(
    lines: Sequence[str], segments: Sequence[Segment], *, source_id_prefix: str
) -> Tuple[List[ProblemSection], List[Dict[str, Any]]]:
    """按行号从原文切片；没有完整样例的题不入库。"""
    sections: List[ProblemSection] = []
    skipped: List[Dict[str, Any]] = []
    for ordinal, segment in enumerate(segments, start=1):
        adjusted = _snap_start(lines, segment)
        block = _trim_trailing_artifacts(list(lines[adjusted.start_line - 1 : adjusted.end_line]))
        samples, _external, warnings = parse_samples(block, section_title=adjusted.title)
        complete = [item for item in samples if item.input_text and item.output_text]
        if not complete:
            skipped.append(
                {
                    "title": adjusted.title,
                    "slug": adjusted.slug,
                    "lines": [adjusted.start_line, adjusted.end_line],
                    "reason": "未抽到完整样例（输入+输出），不入库",
                    "warnings": warnings[:3],
                    "figure_ids": list(adjusted.figure_ids),
                }
            )
            continue
        sections.append(
            ProblemSection(
                ordinal=ordinal,
                title=adjusted.title,
                slug=adjusted.slug,
                lines=block,
                source_id=f"{source_id_prefix}-{adjusted.slug}",
            )
        )
    return sections, skipped


def _attach_figures(
    doc: Any,
    sections: Sequence[ProblemSection],
    segments: Sequence[Segment],
    blocks: Sequence[FigureBlock],
    *,
    out_dir: Path,
    url_prefix: str,
) -> Tuple[int, List[str]]:
    """只为"能入库的题"裁图，返回 (产出图数, 告警)。

    被跳过的题（没有完整样例）不裁图，避免磁盘上留下一堆没人引用的 PNG。
    """
    warnings: List[str] = []
    by_slug = {segment.slug: segment for segment in segments}
    wanted: List[str] = []
    for section in sections:
        segment = by_slug.get(section.slug)
        if segment:
            wanted.extend(segment.figure_ids)

    # 模型偶尔漏判：把没被任何题引用的图块说出来，让用户知道少了一张，而不是静默丢掉
    unused = [block for block in blocks if block.id not in set(wanted)]
    if unused:
        preview = "、".join(f"{block.id}（第{block.page}页）" for block in unused[:5])
        warnings.append(
            f"{len(unused)} 个图块未被任何题目引用：{preview}"
            "。可能是页眉装饰，也可能是模型漏判，请核对"
        )
    if not wanted:
        return 0, warnings

    by_id = {block.id: block for block in blocks}
    chosen = [by_id[figure_id] for figure_id in dict.fromkeys(wanted) if figure_id in by_id]
    if not chosen:
        return 0, warnings
    try:
        records = export_figures(doc, chosen, out_dir, url_prefix=url_prefix)
    except Exception as exc:  # noqa: BLE001 - 裁图失败不该把整卷入库拖死
        return 0, [f"图片裁剪失败：{type(exc).__name__}: {exc}"]
    indexed = {record["id"]: record for record in records}

    total = 0
    for section in sections:
        segment = by_slug.get(section.slug)
        if not segment:
            continue
        figures: List[Dict[str, Any]] = []
        for figure_id in segment.figure_ids:
            record = indexed.get(figure_id)
            if not record:
                continue
            item = dict(record)
            # 每张图优先用自己位置；模型只给了一个总位置时退回 after_line。
            # 转题面内相对行号：1 = 标题之后、正文之前
            absolute = segment.figure_positions.get(figure_id) or segment.after_line
            item["after_line"] = max(1, absolute - segment.start_line + 1) if absolute else 1
            figures.append(item)
        section.figures = figures
        total += len(figures)
        if segment.figure_missing and not figures:
            warnings.append(
                f"{section.title}：题面提到图但未匹配到图块，已入库但需人工补图"
                "（模型标记 figure_missing）"
            )
    return total, warnings


def ingest_pdf_text(
    text: str,
    *,
    db: Any,
    client: Any,
    source: str = SOURCE_NAME,
    path_label: str = "<pdf>",
) -> PdfIngestResult:
    """文本已在内存时的入口（测试与 CLI 共用；无图，纯文本路径）。"""
    segments, warnings = segment_with_llm(client, text)
    lines = text.splitlines()
    if len(lines) > MAX_SEGMENT_LINES:
        lines = lines[:MAX_SEGMENT_LINES]
    stem = _source_prefix(Path(path_label).stem or "pdf", text)
    sections, skipped = build_sections(lines, segments, source_id_prefix=stem)
    report = IngestReport(source=source, path=path_label)
    report.warnings.extend(warnings)
    if not sections:
        report.warnings.append("没有可入库的题目（全部被跳过或切题失败）")
        return PdfIngestResult(report=report, segments=segments, warnings=warnings, skipped=skipped)
    ingest_sections(sections, db=db, source=source, path_label=path_label, report=report)
    return PdfIngestResult(report=report, segments=segments, warnings=warnings, skipped=skipped)


def ingest_pdf_file(
    path: Path | str,
    *,
    db: Any,
    client: Any,
    source: str = SOURCE_NAME,
    use_vision: bool = True,
    assets_root: Path | str | None = None,
) -> PdfIngestResult:
    """整卷入口：一次打开 PDF，文本、页图、图块都从这里产出。

    `use_vision=False` 是保留的退回开关（等价于旧行为，只发文本）。
    """
    pymupdf = _pymupdf()
    file_path = Path(path)
    raw_stem = file_path.stem or "pdf"
    root = Path(assets_root) if assets_root else ASSETS_ROOT

    warnings: List[str] = []
    with pymupdf.open(str(file_path)) as doc:
        # 逐页抽文本并清页眉页脚；切题与页-行映射都用这份清洗后的文本，
        # 否则图块归属校验会拿清洗前的行号去比清洗后的题号区间，整批误杀。
        page_texts = [_drop_page_furniture(page.get_text()) for page in doc]
        text = "\n".join(page_texts)
        # CSS 前缀要在拿到正文后才能定（文件名里没有编号时靠「第 N 次认证」兜底）
        stem = _source_prefix(raw_stem, text)
        out_dir = root / stem
        url_prefix = f"/assets/{stem}"
        lines = text.splitlines()
        if len(lines) > MAX_SEGMENT_LINES:
            lines = lines[:MAX_SEGMENT_LINES]
            text = "\n".join(lines)

        blocks, block_warnings = enum_figures(doc)
        warnings.extend(block_warnings)

        page_images: List[Tuple[int, bytes]] = []
        if use_vision and blocks:
            if int(doc.page_count) > MAX_PAGE_IMAGES:
                warnings.append(
                    f"共 {doc.page_count} 页，超过单次送图上限 {MAX_PAGE_IMAGES}，"
                    "本轮只送文本，图片归属需人工核对"
                )
            else:
                try:
                    page_images = [
                        (page_index + 1, render_page_jpeg(doc, page_index))
                        for page_index in range(int(doc.page_count))
                    ]
                except Exception as exc:  # noqa: BLE001 - 渲染失败退回纯文本
                    warnings.append(
                        f"页图渲染失败，已退回纯文本切题：{type(exc).__name__}: {exc}"
                    )
        elif use_vision:
            warnings.append("本卷未检测到图块，按纯文本切题")

        segments, seg_warnings = segment_with_llm(
            client,
            text,
            page_images=page_images,
            blocks=blocks,
            page_ranges=line_ranges(page_texts),
        )
        warnings.extend(seg_warnings)

        sections, skipped = build_sections(lines, segments, source_id_prefix=stem)
        figure_total, figure_warnings = _attach_figures(
            doc, sections, segments, blocks, out_dir=out_dir, url_prefix=url_prefix
        )
        warnings.extend(figure_warnings)

    report = IngestReport(source=source, path=str(file_path))
    report.warnings.extend(warnings)
    if not sections:
        report.warnings.append("没有可入库的题目（全部被跳过或切题失败）")
        return PdfIngestResult(
            report=report,
            segments=segments,
            warnings=warnings,
            skipped=skipped,
            figures_total=0,
            page_images=len(page_images),
        )
    ingest_sections(sections, db=db, source=source, path_label=str(file_path), report=report)
    return PdfIngestResult(
        report=report,
        segments=segments,
        warnings=warnings,
        skipped=skipped,
        figures_total=figure_total,
        page_images=len(page_images),
    )
