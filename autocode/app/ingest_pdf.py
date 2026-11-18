"""整卷 PDF → LLM 切题 → 原文切片入库（B 方案，用户 2026-11-17 定案）。

职责边界（关键）：
- LLM **只决定边界**（title / slug / start_line / end_line），不输出题面正文，更不输出样例；
- 题面与样例由代码按行号从**原始文本**逐字切片，模型写的正文一律不采用；
- 每题至少 1 组完整样例，否则不入库并列出原因（宁缺勿错）。

为什么不是“LLM 直接输出题面”：一旦正文/样例经过模型转述，
后续 grammar 回放、判题、对拍全部建立在改写过的文本上，错得无声无息。
"""

from __future__ import annotations

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

SOURCE_NAME = "CSP-PDF"
#: 单次送给模型的行数上限（16 页试卷约 500~800 行；超过则截断并明确告警）。
MAX_SEGMENT_LINES = 4000
#: 一题至少要有这么多行，避免模型切出只有标题/页眉的空段。
MIN_SEGMENT_LINES = 4

_TITLE_SUFFIX_RE = re.compile(r"（([a-z0-9][a-z0-9_-]*)）\s*$")
_CERT_PREFIX_RE = re.compile(r"^第\s*\d+\s*次认证")
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass
class Segment:
    """LLM 给出的题目边界（行号 1-based，闭区间）。"""

    title: str
    slug: str
    start_line: int
    end_line: int


@dataclass
class PdfIngestResult:
    report: IngestReport
    segments: List[Segment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # 去重：切题告警既进 result.warnings 又进 report.warnings（ingest 报告统一展示），
        # 直接拼接会让同一条在界面上出现两次。
        warnings = list(dict.fromkeys([*self.warnings, *self.report.warnings]))
        return {
            "path": self.report.path,
            "segments": [seg.__dict__ for seg in self.segments],
            "imported": [
                {
                    "id": item.problem_id,
                    "title": item.title,
                    "source_id": item.source_id,
                    "samples": len(item.samples),
                    "created": item.created,
                }
                for item in self.report.problems
            ],
            "skipped": self.skipped,
            "warnings": warnings,
        }


def extract_pdf_text(path: Path | str) -> str:
    """抽取 PDF 纯文本。pymupdf 是硬依赖（见 requirements.txt）。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover - 老版本包名
        import fitz as pymupdf  # type: ignore
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
            return Segment(segment.title, segment.slug, probe + 1, max(segment.end_line, probe + 2))
    return segment


def parse_segments(payload: Dict[str, Any], total_lines: int) -> Tuple[List[Segment], List[str]]:
    """校验并归一 LLM 返回的边界：越界/倒序/重叠一律丢弃并说明。"""
    warnings: List[str] = []
    items = payload.get("problems") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], ["模型未返回 problems 数组"]
    candidates: List[Segment] = []
    for ordinal, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = _clean_title(item.get("title") or "")
        slug = _clean_slug(item.get("slug") or "", ordinal)
        try:
            start = int(item.get("start_line"))
            end = int(item.get("end_line"))
        except (TypeError, ValueError):
            warnings.append(f"{title or f'第{ordinal}题'}：行号不是整数，已丢弃")
            continue
        if not (1 <= start <= end <= total_lines):
            warnings.append(f"{title or slug}：行号 {start}~{end} 越界（1~{total_lines}），已丢弃")
            continue
        if end - start + 1 < MIN_SEGMENT_LINES:
            warnings.append(f"{title or slug}：区间只有 {end - start + 1} 行，疑似误切，已丢弃")
            continue
        candidates.append(Segment(title or slug, slug, start, end))

    candidates.sort(key=lambda seg: seg.start_line)
    kept: List[Segment] = []
    seen_slug = set()
    for segment in candidates:
        if segment.slug in seen_slug:
            warnings.append(f"{segment.title}：slug={segment.slug} 重复，已丢弃后一条")
            continue
        if kept and segment.start_line <= kept[-1].end_line:
            warnings.append(
                f"{segment.title}：与上一题区间重叠"
                f"（{segment.start_line} <= {kept[-1].end_line}），已丢弃"
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


def segment_with_llm(
    client: Any, text: str, *, tag: str = "segment_pdf#1"
) -> Tuple[List[Segment], List[str]]:
    """请模型标边界；返回 (segments, warnings)。不做任何入库。"""
    lines = text.splitlines()
    warnings: List[str] = []
    if len(lines) > MAX_SEGMENT_LINES:
        warnings.append(
            f"文本 {len(lines)} 行超过上限 {MAX_SEGMENT_LINES}，仅送前 {MAX_SEGMENT_LINES} 行；"
            "后面的题目不会被切出"
        )
        lines = lines[:MAX_SEGMENT_LINES]
    system_text = prompts.load(prompts.FILE_SEGMENT)
    user_text = (
        f"总行数：{len(lines)}\n\n"
        "以下是整卷文本（每行 `行号| 原文`）：\n\n"
        + _numbered(lines)
    )
    result = client.chat(
        [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        json_mode=True,
        max_tokens=4096,
        temperature=0.0,
        tag=tag,
        stage="segment",
        problem_id=None,
        prompt_hash=hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16],
    )
    segments, problems = parse_segments(result.json_obj or {}, len(lines))
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


def ingest_pdf_text(
    text: str,
    *,
    db: Any,
    client: Any,
    source: str = SOURCE_NAME,
    path_label: str = "<pdf>",
) -> PdfIngestResult:
    """文本已在内存时的入口（测试与 CLI 共用）。"""
    segments, warnings = segment_with_llm(client, text)
    lines = text.splitlines()
    if len(lines) > MAX_SEGMENT_LINES:
        lines = lines[:MAX_SEGMENT_LINES]
    stem = Path(path_label).stem or "pdf"
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
) -> PdfIngestResult:
    file_path = Path(path)
    text = extract_pdf_text(file_path)
    return ingest_pdf_text(text, db=db, client=client, source=source, path_label=str(file_path))
