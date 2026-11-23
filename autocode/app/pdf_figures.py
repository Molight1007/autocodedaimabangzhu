"""PDF 图块枚举、页图渲染与裁剪。

为什么坐标不交给模型：视觉模型返回的 bbox 是估算值，页图被缩放到约 1300px 后
误差可达几十 pt，裁出来边角必缺。这里反过来——**代码枚举全部图块并编号**，
模型只回答"哪几个编号属于这道题"。语义判断归模型，几何精度归代码。

图块来源两条，缺一不可：
1. `page.get_images()` → 嵌入的位图（照片、扫描图）；
2. `page.get_drawings()` → 矢量路径。CSP 试卷里的树、图、坐标轴多数是**矢量画的**，
   `get_images()` 一张也抓不到，必须按合并后的 bbox 渲染成位图。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: 合并后宽或高小于该值（pt）的簇视为装饰线/字符笔画，丢弃。
MIN_SIDE_PT = 36.0
#: 相邻两个矢量矩形间距小于该值就并入同一图块（一张树图的节点框间距约 20~40pt）。
MERGE_GAP_PT = 14.0
#: 单页矢量路径超过该数量时放弃聚类（避免 O(n²) 卡死），退化为整页裁剪。
MAX_DRAWINGS = 4000
#: 与已有图块 IoU 超过该值视为同一块（矢量描边 + 位图填充会重复出现）。
IOU_DEDUP = 0.85
#: 裁剪时四周留白（pt），避免把边框线裁掉一半。
CROP_PAD_PT = 6.0
#: 图块内若有单行文字长度达到该值，判定为"正文区"而非插图（见 `_looks_like_text`）。
TEXT_LINE_PT = 20
#: 簇内宽高都超过该值的图形元素少于这个数，判定为装饰线（页边线、分隔线、公式横线）。
MIN_SHAPES = 3
SHAPE_SIDE_PT = 5.0


def _pymupdf():
    """延迟导入：测试环境可能没装。"""
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:  # pragma: no cover - 老版本包名
        import fitz as pymupdf  # type: ignore

        return pymupdf


@dataclass
class FigureBlock:
    """页面上的一个图块（坐标单位 pt，原点左上）。"""

    id: str
    page: int  # 1-based
    bbox: Tuple[float, float, float, float]
    kind: str  # bitmap | vector | page
    width: float
    height: float

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "bbox": [round(v, 1) for v in self.bbox],
            "kind": self.kind,
            "width": round(self.width, 1),
            "height": round(self.height, 1),
        }


def _rect_tuple(rect: Any) -> Tuple[float, float, float, float]:
    return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))


def _make_rect(values: Sequence[float]) -> Any:
    return _pymupdf().Rect(*[float(v) for v in values])


def _union(rects: Sequence[Any]) -> Any:
    """多个矩形求并集（用 pymupdf 的 `|` 运算符，避免手写 min/max 出错）。"""
    merged = rects[0]
    for rect in rects[1:]:
        merged = merged | rect
    return merged


def _near(a: Any, b: Any, gap: float) -> bool:
    return not (
        a.x1 + gap < b.x0
        or b.x1 + gap < a.x0
        or a.y1 + gap < b.y0
        or b.y1 + gap < a.y0
    )


def _iou(a: Any, b: Any) -> float:
    inter = a & b
    if inter.is_empty:
        return 0.0
    union_area = a.get_area() + b.get_area() - inter.get_area()
    if union_area <= 0:
        return 0.0
    return float(inter.get_area()) / float(union_area)


def cluster_rects(
    rects: Sequence[Any], gap: float = MERGE_GAP_PT
) -> List[Tuple[Any, List[Any]]]:
    """把相邻/相交的矩形聚成簇，返回 [(并集 bbox, 簇内原始矩形)]。

    并查集实现：一张树图由几十个节点框 + 连线组成，逐条当图块会产生几十个碎片。
    先按 y0 排序，只与"纵向还挨得上"的后继比较，把 O(n²) 压到近似 O(n·k)。
    """
    items = [r for r in rects if float(r.width) > 0.3 or float(r.height) > 0.3]
    if not items:
        return []
    items.sort(key=lambda r: (float(r.y0), float(r.x0)))
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i, left in enumerate(items):
        for j in range(i + 1, len(items)):
            right = items[j]
            # 已按 y0 排序：后面的框只要 y0 超出 left 的底部 + gap，就不可能再相邻。
            if float(right.y0) > float(left.y1) + gap:
                break
            if _near(left, right, gap):
                union(i, j)

    groups: Dict[int, List[Any]] = {}
    for index, rect in enumerate(items):
        groups.setdefault(find(index), []).append(rect)
    return [(_union(members), members) for members in groups.values()]


def _shape_count(members: Sequence[Any]) -> int:
    """簇内"像图形"的元素数：宽高都超过阈值的矩形/路径才算，细线不算。"""
    return sum(
        1
        for r in members
        if float(r.width) >= SHAPE_SIDE_PT and float(r.height) >= SHAPE_SIDE_PT
    )


def _page_lines(page: Any) -> List[Tuple[Any, int]]:
    """提取每行文本的 bbox 与字符数，用于区分"插图"与"正文区"。"""
    pymupdf = _pymupdf()
    out: List[Tuple[Any, int]] = []
    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001 - 取不到文本时该判据自动失效，不影响主流程
        return out
    for block in data.get("blocks", []):
        if int(block.get("type", 0)) != 0:
            continue
        for line in block.get("lines", []):
            bbox = None
            chars = 0
            for span in line.get("spans", []):
                text = str(span.get("text") or "")
                if not text.strip():
                    continue
                rect = pymupdf.Rect(span.get("bbox"))
                bbox = rect if bbox is None else (bbox | rect)
                chars += len(text.strip())
            if bbox is not None:
                out.append((bbox, chars))
    return out


def _looks_like_text(rect: Any, lines: Sequence[Tuple[Any, int]]) -> bool:
    """图块里是否压着成段正文。

    实测（`离散数学作业.pdf` 第 9 页）：页边竖线 + 公式上划线被聚成一块，
    bbox 覆盖了整栏文字。仅靠尺寸/长宽比拦不住（比值为 5.5），
    但"块内存在一条 ≥20 字的文本行"能稳定识别。
    真正的插图里只有节点编号、坐标轴刻度这类短标注，不会出现长行。
    """
    for line_rect, chars in lines:
        if chars < TEXT_LINE_PT:
            continue
        cx = (float(line_rect.x0) + float(line_rect.x1)) / 2.0
        cy = (float(line_rect.y0) + float(line_rect.y1)) / 2.0
        if float(rect.x0) <= cx <= float(rect.x1) and float(rect.y0) <= cy <= float(rect.y1):
            return True
    return False


def _page_usable(rect: Any, page_rect: Any, min_side: float) -> bool:
    """过滤装饰线与背景：太小、几乎占满整页、或细长到不像图的一律丢。"""
    width, height = float(rect.width), float(rect.height)
    if width < min_side or height < min_side:
        return False
    page_area = float(page_rect.width) * float(page_rect.height)
    if page_area <= 0:
        return False
    if float(rect.get_area()) / page_area > 0.70:
        return False
    # 页眉分隔线：整页宽但只有几 pt 高（上面的 min_side 已能拦住，这里防合并后变高）
    if width > float(page_rect.width) * 0.95 and height < 24:
        return False
    return True


def enum_page_blocks(
    page: Any,
    *,
    page_no: int,
    min_side: float = MIN_SIDE_PT,
    gap: float = MERGE_GAP_PT,
) -> Tuple[List[Tuple[Any, str]], List[str]]:
    """枚举单页图块，返回 ([(rect, kind)], warnings)。"""
    pymupdf = _pymupdf()
    warnings: List[str] = []
    page_rect = page.rect
    found: List[Tuple[Any, str]] = []

    # 1) 嵌入位图
    try:
        for info in page.get_images(full=True):
            xref = int(info[0])
            for rect in page.get_image_rects(xref):
                candidate = pymupdf.Rect(rect)
                if _page_usable(candidate, page_rect, min_side):
                    found.append((candidate, "bitmap"))
    except Exception as exc:  # noqa: BLE001 - 单页失败不该中断整卷
        warnings.append(f"第{page_no}页位图枚举失败：{type(exc).__name__}: {exc}")

    # 2) 矢量路径聚类
    try:
        drawings = page.get_drawings()
    except Exception as exc:  # noqa: BLE001
        drawings = []
        warnings.append(f"第{page_no}页矢量枚举失败：{type(exc).__name__}: {exc}")

    if len(drawings) > MAX_DRAWINGS:
        warnings.append(
            f"第{page_no}页矢量路径 {len(drawings)} 条超过上限 {MAX_DRAWINGS}，"
            "改用整页图，不再细分图块"
        )
        found.append((pymupdf.Rect(page_rect), "page"))
    else:
        rects = [d.get("rect") for d in drawings if d.get("rect") is not None]
        lines = _page_lines(page)
        for cluster, members in cluster_rects(rects, gap=gap):
            if not _page_usable(cluster, page_rect, min_side):
                continue
            # 页边线、分隔线、公式上划线只有寥寥几笔，凑不成图。
            # 例外：很多细线也能拼出图（纯线条画的有向图），故用元素总数兜底。
            if _shape_count(members) < MIN_SHAPES and len(members) < 12:
                continue
            # 簇里压着成段正文 → 这是页面排版不是插图。
            if _looks_like_text(cluster, lines):
                continue
            found.append((pymupdf.Rect(cluster), "vector"))

    # 3) 去重：位图与矢量描边常描述同一张图
    kept: List[Tuple[Any, str]] = []
    for rect, kind in found:
        if any(_iou(rect, other) >= IOU_DEDUP for other, _ in kept):
            continue
        kept.append((rect, kind))
    kept.sort(key=lambda item: (float(item[0].y0), float(item[0].x0)))
    return kept, warnings


def enum_figures(doc: Any, *, min_side: float = MIN_SIDE_PT) -> Tuple[List[FigureBlock], List[str]]:
    """枚举整卷图块并编号 `fig_1`、`fig_2`……；编号即模型可引用的唯一凭据。"""
    blocks: List[FigureBlock] = []
    warnings: List[str] = []
    for page_index in range(int(doc.page_count)):
        page = doc.load_page(page_index)
        page_blocks, page_warnings = enum_page_blocks(
            page, page_no=page_index + 1, min_side=min_side
        )
        warnings.extend(page_warnings)
        for rect, kind in page_blocks:
            blocks.append(
                FigureBlock(
                    id=f"fig_{len(blocks) + 1}",
                    page=page_index + 1,
                    bbox=_rect_tuple(rect),
                    kind=kind,
                    width=float(rect.width),
                    height=float(rect.height),
                )
            )
    return blocks, warnings


def line_ranges(texts: Sequence[str]) -> List[Tuple[int, int]]:
    """多段文本拼成整卷后，每段占据的行号区间（1-based，闭区间）。

    必须按**字符位置**换算行号：`"\\n".join(段)` 会在段间多出一个换行，
    直接累加每段 `splitlines()` 的数量会越算越偏。

    调用方要保证传入的 `texts` 与切题用的文本是同一份（清洗过就都清洗），
    否则行号对不上，图块归属校验会整批误杀。
    """
    full = "\n".join(texts)
    ranges: List[Tuple[int, int]] = []
    cursor = 0
    for index, text in enumerate(texts):
        start_line = full.count("\n", 0, cursor) + 1
        end_cursor = cursor + len(text)
        # 段文本通常以 "\n" 结尾，直接用 end_cursor 数换行会把下一行算进来；退一格即可
        end_line = full.count("\n", 0, max(cursor, end_cursor - 1)) + 1
        ranges.append((start_line, max(start_line, end_line)))
        cursor = end_cursor + (1 if index < len(texts) - 1 else 0)
    return ranges


def page_line_ranges(doc: Any) -> List[Tuple[int, int]]:
    """每页**原始文本**在整卷行号里的区间（未做页眉清洗，仅供独立诊断用）。"""
    return line_ranges([page.get_text() for page in doc])


def page_text_lines(doc: Any) -> List[List[Tuple[float, float, str]]]:
    """每页文字行的 `(y0, y1, 文本)`，顺序与 `page.get_text()` 的行一致。

    用途：把图块的顶边对到"它上方最近的一行文字"，算出图该插在哪一行之后。
    只按页码归属是不够的——一页的图可能画在页面下半部，按页码会算成"页首"。
    实测 CSP32 第 6 页的树图画在 `下图示意性地说明了…` 之后，按页码得到的却是
    题面第 1 行（页首），图会跑到标题下面。

    注意 `get_text()` 与 `get_text("dict")` 都会跳过空白行，所以这里只返回有字的行，
    调用方必须按**文本内容**回查清洗后的行号，不能按索引对齐。
    """
    pages: List[List[Tuple[float, float, str]]] = []
    for page in doc:
        lines: List[Tuple[float, float, str]] = []
        for block in page.get_text("dict").get("blocks", []):
            if int(block.get("type", -1)) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                if not text.strip():
                    continue
                bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                lines.append((float(bbox[1]), float(bbox[3]), text))
        pages.append(lines)
    return pages


def render_page_jpeg(
    doc: Any, page_index: int, *, dpi: int = 150, quality: int = 80
) -> bytes:
    """整页渲染成 JPEG（给视觉模型看版面）。

    PNG 一页通常 1~2MB，16 页就顶到请求体上限；JPEG q80 约 200~400KB。
    模型只需要看清"哪里有一张图、它挨着哪段文字"，不需要读小字——文字由文本层给。
    """
    pymupdf = _pymupdf()
    page = doc.load_page(int(page_index))
    pix = page.get_pixmap(dpi=int(dpi), colorspace=pymupdf.csRGB, alpha=False)
    return pix.tobytes("jpeg", jpg_quality=int(quality))


def crop_figure_png(doc: Any, block: FigureBlock, *, dpi: int = 200, pad: float = CROP_PAD_PT) -> bytes:
    """按图块 bbox 从原 PDF 裁剪，输出 PNG（原始像素，不经模型转述）。"""
    pymupdf = _pymupdf()
    page = doc.load_page(int(block.page) - 1)
    page_rect = page.rect
    x0 = max(float(page_rect.x0), float(block.bbox[0]) - pad)
    y0 = max(float(page_rect.y0), float(block.bbox[1]) - pad)
    x1 = min(float(page_rect.x1), float(block.bbox[2]) + pad)
    y1 = min(float(page_rect.y1), float(block.bbox[3]) + pad)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"{block.id} 的 bbox 无有效面积：{block.bbox}")
    clip = pymupdf.Rect(x0, y0, x1, y1)
    pix = page.get_pixmap(clip=clip, dpi=int(dpi), alpha=False)
    return pix.tobytes("png")


def export_figures(
    doc: Any,
    blocks: Iterable[FigureBlock],
    out_dir: Path | str,
    *,
    url_prefix: str = "",
    dpi: int = 200,
) -> List[Dict[str, Any]]:
    """把图块裁剪落盘，返回可直接存库的记录（含相对路径）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    for block in blocks:
        data = crop_figure_png(doc, block, dpi=dpi)
        filename = f"{block.id}.png"
        (target / filename).write_bytes(data)
        record = block.to_dict()
        record["path"] = f"{url_prefix.rstrip('/')}/{filename}" if url_prefix else filename
        record["bytes"] = len(data)
        records.append(record)
    return records
