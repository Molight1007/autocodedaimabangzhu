"""D1 切题：模型只给行号，题面由程序从原文切片。

这是与 v1 最大的差别。v1 让模型复述题面，`10^9` 被写成 `109` 也无人察觉；
v2 的契约里没有题面字段，模型想改也没有地方写。

送模型前先删页眉/页脚/页码：这些行不属于题面，留着会跟着行号一起被切进去。
切完不直接入库：先落 `import_segments`，人工勾选确认后才写 `problems`。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..app.ingest_csp import parse_samples
from . import banks, config, problem_purge
from .llm_stage import StageRunner
from .stages import SEGMENT
from .validators import check_segments, judge_supported

#: 整份输入的下限：低于这个长度不可能有任何题目，用于快速失败，省掉一次模型调用。
#: 注意**只作用于整份输入**，不用于单道题——判断"这一段算不算一道题"是模型的活，
#: 代码不该用字数替它做决定（曾经用 100 字卡单题，把 95 字的「两数之和」静默丢掉了）。
MIN_STATEMENT_CHARS = 100

#: 题干标题行：`宝藏（treasure）`。整行只有"名字 + 括号里的 slug"，没有空格。
#: 页眉的 `第32 次认证宝藏（treasure）` 因为名字里有空格，不会被误认。
_HEADING_RE = re.compile(r"^([^（）()\s]{1,40})[（(]([a-z0-9][a-z0-9_-]{0,30})[）)]$")
#: 模型给的起点偏离标题行超过这么多行就不吸附——宁可不动，也不猜。
SNAP_WINDOW = 45
_SLUG_IN_TITLE_RE = re.compile(r"[（(]([a-z0-9][a-z0-9_-]{0,30})[）)]\s*$")


def _title_slug(title: Any) -> str:
    match = _SLUG_IN_TITLE_RE.search(str(title or "").strip())
    return match.group(1) if match else ""


def snap_to_headings(items: List[Dict[str, Any]], lines: Sequence[str],
                     window: int = SNAP_WINDOW) -> int:
    """把每道题的起点吸附到题干标题行，并用新起点重算上一题的结束行。

    为什么要做：模型对"这道题从哪一行开始"经常差几十行（实测 CSP32 把「宝藏」
    提前到上一题的解释段里，连带 37 行正文被算进下一题）。行号一偏，图就会按
    页码挂到错的题上——题是对的，图是别人的，比缺图更难发现。

    标题行 `宝藏（treasure）` 是代码能确定性认出来的唯一锚点，所以只信它：
    slug 取自模型自己给的标题，距离超过 window 或吸附后不再递增，一律保留原值。
    返回吸附成功的题数。
    """
    index: Dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line.strip())
        if match and match.group(2) not in index:
            index[match.group(2)] = number
    if not index:
        return 0

    starts: List[int] = []
    for item in items:
        current = int(item["start_line"])
        target = index.get(_title_slug(item.get("title")))
        usable = target is not None and target != current and abs(target - current) <= window
        starts.append(target if usable else current)

    # 吸附后必须仍然严格递增，否则说明锚点比模型给的还乱，整批放弃
    if any(right <= left for left, right in zip(starts, starts[1:])):
        return 0

    snapped = sum(1 for item, start in zip(items, starts) if start != int(item["start_line"]))
    if not snapped:
        return 0

    for position, (item, start) in enumerate(zip(items, starts)):
        item["start_line"] = start
        if position + 1 < len(starts):
            item["end_line"] = starts[position + 1] - 1
        elif int(item["end_line"]) < start:
            item["end_line"] = len(lines)
    return snapped


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(raw: str) -> str:
    text = str(raw or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


#: 页眉/页脚/页码：整行匹配才删，避免误伤正文里提到"第 3 页"的句子。
_PAGE_FURNITURE_RE = re.compile(
    r"^(?:"
    r"第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?"
    r"|共\s*\d+\s*页"
    r"|CCF\s*CSP.*"
    r"|第\s*\d+\s*次认证.*"
    r"|<!--\s*Page\s*\d+\s*-->"
    r")$"
)


def is_page_furniture(line: str) -> bool:
    """这行是不是页眉/页脚/页码（整行匹配才算）。"""
    return bool(_PAGE_FURNITURE_RE.match(str(line).strip()))


def strip_page_furniture(lines: Sequence[str]) -> Tuple[List[str], List[str]]:
    """删掉整行的页眉/页脚/页码，返回 (保留行, 删除行)。

    只删整行，保留空行和原始缩进：题面由程序逐字切片，不能顺手 strip 每一行。
    """
    kept: List[str] = []
    removed: List[str] = []
    for line in lines:
        if is_page_furniture(line):
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def number_lines(lines: Sequence[str]) -> str:
    width = max(4, len(str(len(lines))))
    return "\n".join(f"{index:0{width}d}| {line}" for index, line in enumerate(lines, start=1))


def slice_lines(lines: Sequence[str], start: int, end: int) -> str:
    """行号是 1-based 闭区间；越界部分自动裁掉。"""
    left = max(0, int(start) - 1)
    right = min(len(lines), int(end))
    return "\n".join(lines[left:right]).strip("\n")


def extract_samples(statement: str) -> List[Dict[str, Any]]:
    try:
        samples, _external, _warnings = parse_samples(statement.split("\n"))
    except Exception:  # noqa: BLE001 - 解析失败不该阻断导入，样例留空后续拒生成
        return []
    out: List[Dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        if not sample.complete:
            continue
        out.append(
            {
                "index": index,
                "input": sample.input_text,
                "output": sample.output_text,
            }
        )
    return out


def refresh_samples(db: Any, problem: Any) -> List[Dict[str, Any]]:
    """题面样例为空时，按**当前**解析规则从 statement_md 重算一次并落库。

    为什么必须补这一步：`samples_json` 是导入那一刻算出来存下的。解析规则升级后，
    老题仍然带着旧结果（0 组）；而重新导入会被 `statement_hash` 判成重复题、
    只返回旧 problem_id，样例不会更新——用户看到的就是"改完了还是不行"。

    只在为空时触发：已有样例的题不动，避免新规则把人工确认过的结果悄悄改掉。
    """
    statement = str(problem["statement_md"] or "")
    samples = extract_samples(statement)
    if not samples:
        return []
    with db.transaction() as conn:
        conn.execute(
            "UPDATE problems SET samples_json=?, updated_at=? WHERE id=?",
            (_json(samples), _now(), int(problem["id"])),
        )
    return samples


def attach_figures_by_page(
    rows: List[Dict[str, Any]],
    figures: Sequence[Dict[str, Any]],
    page_ranges: Sequence[Sequence[int]],
) -> List[str]:
    """按几何位置把 PDF 图块分给题目：不调模型，只看行号区间。

    规则：图在第 P 页 → 该页的行号区间是 `page_ranges[P-1]`，
    分给"题目行号区间与该页交集最大"的那道题。CSP 试卷基本一题一页，
    交集就是整页；一页多题时取交集最大者（正文落在哪页，图就属于那道题）。

    同一页的多张图会在 `after_line` 上依次 +1，避免全挤在同一行。
    返回告警（没有题目覆盖的页、图块被跳过）。
    """
    warnings: List[str] = []
    if not figures or not page_ranges:
        return warnings
    used: Dict[int, int] = {}
    for figure in figures:
        figure_id = str(figure.get("id") or "")
        try:
            page = int(figure.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= page <= len(page_ranges)):
            continue
        page_start = int(page_ranges[page - 1][0])
        page_end = int(page_ranges[page - 1][1])
        best_row: Optional[Dict[str, Any]] = None
        best_overlap = 0
        for row in rows:
            start, end = int(row["start_line"]), int(row["end_line"])
            overlap = min(page_end, end) - max(page_start, start) + 1
            if overlap > best_overlap:
                best_row, best_overlap = row, overlap
        if best_row is None:
            warnings.append(
                f"{figure_id} 在第{page}页（行{page_start}-{page_end}），"
                "没有题目覆盖这一页，已跳过"
            )
            continue
        start = int(best_row["start_line"])
        # 优先用"图上方的文字行"算插入位置：只按页码会把图一律放到页首，
        # 实测 CSP32 第 6 页的树图画在下半页，按页码得到的是题面第 1 行。
        anchor = int(figure.get("anchor_line") or 0)
        total = len(str(best_row.get("statement_md") or "").split("\n"))
        if anchor > 0:
            after = anchor - start + 1
            after = max(1, min(after, max(1, total)))
        else:
            # 图在本页最上方（上方只有页眉）：应插在"本页第一行之前"，
            # 而不是本页第一行之后——后者会把图压到图内文字下面
            after = max(1, max(start, page_start) - start)
        key = start
        # 同一题的多张图必须严格递增，否则前端排序不稳定、图会挤在一起
        previous = used.get(key, 0)
        if previous:
            after = max(after, previous + 1)
        used[key] = after
        best_row.setdefault("figures", []).append(
            {
                "id": figure_id,
                "page": page,
                "bbox": figure.get("bbox") or [],
                "width": float(figure.get("width") or 0),
                "height": float(figure.get("height") or 0),
                "after_line": after,
            }
        )
    return warnings


def normalize_layout(blocks: List[Dict[str, Any]], lines: Sequence[str]) -> Tuple[List[Dict[str, Any]], int]:
    """把模型给的块整理成"没有裂缝、也不过分碎"的样子。

    两件事，都是模型做不到、只能由代码保证的：

    1. **补缝**：模型偶尔漏标几行（实测 CSP32 有题漏 16 行）。漏掉的行不属于任何块，
       前端就只能猜；这里按正文补成 `para`，顺序不变、文字不丢。
    2. **合并**：模型也常把一段话拆成十几块（实测 192 行给了 130 块），
       相邻的 `para` 合并回一段，否则渲染出来是一堆各自成段的碎句。

    返回 (整理后的块, 补过的行数)。
    """
    total = len(lines)
    filled: List[Dict[str, Any]] = []
    inserted = 0
    cursor = 1

    for block in blocks:
        start = int(block["from"])
        if start > cursor:
            # 第 1 行是题名，标题栏已经有了，不能又当正文补一遍
            gap_start = max(cursor, 2)
            if gap_start < start and any(
                str(lines[number - 1]).strip() for number in range(gap_start, start)
            ):
                filled.append({"kind": "para", "from": gap_start, "to": start - 1})
                inserted += start - gap_start
        filled.append(dict(block))
        cursor = max(cursor, int(block["to"]) + 1)

    if cursor <= total and any(str(lines[number - 1]).strip() for number in range(cursor, total + 1)):
        filled.append({"kind": "para", "from": cursor, "to": total})
        inserted += total - cursor + 1

    merged: List[Dict[str, Any]] = []
    for block in filled:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and block["kind"] == "para"
            and previous["kind"] == "para"
            and int(previous["to"]) + 1 == int(block["from"])
        ):
            previous["to"] = block["to"]
            continue
        merged.append(block)
    return merged, inserted


def _slice_statement(item: Dict[str, Any], lines: Sequence[str]) -> Tuple[List[str], Dict[int, int], int]:
    """切出题面正文：去掉模型标为"不属于题面"的行与页眉页脚。

    返回 `(正文行, 旧相对行号→新相对行号, 删掉的行数)`。映射给分块用：
    删行之后块坐标必须跟着挪，否则排版会把正文挂到别的小节下面。
    """
    start, end = int(item["start_line"]), int(item["end_line"])
    skip = set(item.pop("skip_lines", None) or [])
    body: List[str] = []
    mapping: Dict[int, int] = {}
    removed = 0
    for absolute in range(start, end + 1):
        text = lines[absolute - 1]
        if absolute in skip or is_page_furniture(text):
            removed += 1
            continue
        mapping[absolute - start + 1] = len(body) + 1
        body.append(text)
    return body, mapping, removed


def _remap_blocks(blocks: Any, mapping: Dict[int, int]) -> Optional[List[Dict[str, Any]]]:
    """把分块坐标换到删行后的题面上；整块都被删掉就丢掉这块。"""
    out: List[Dict[str, Any]] = []
    for block in blocks or []:
        hits = [
            mapping[number]
            for number in range(int(block["from"]), int(block["to"]) + 1)
            if number in mapping
        ]
        if hits:
            out.append({**block, "from": min(hits), "to": max(hits)})
    return out or None


def segment_text(
    db: Any,
    runner: StageRunner,
    text: str,
    *,
    source_name: str = "",
    kind: str = "text",
    figures: Optional[Sequence[Dict[str, Any]]] = None,
    page_ranges: Optional[Sequence[Sequence[int]]] = None,
    pdf_key: str = "",
    bank_id: int = banks.DEFAULT_ID,
    role: str = "student",
) -> Dict[str, Any]:
    # 先判库和权限，再花模型的钱：学生选了永久库就直接回去，不白烧一次调用。
    bank = banks.get(db, bank_id)
    if bank is None:
        return {"error": "题库不存在", "code": "NOT_FOUND"}
    if not banks.can_manage(bank, role):
        return {"error": banks.DENY_ADD, "code": "FORBIDDEN"}
    cleaned = clean_text(text)
    if len(cleaned) < MIN_STATEMENT_CHARS:
        return {"error": f"文本太短（{len(cleaned)} 字符），至少 {MIN_STATEMENT_CHARS} 字符", "code": "INVALID_INPUT"}
    if len(cleaned) > config.MAX_IMPORT_CHARS:
        return {
            "error": f"文本超过 {config.MAX_IMPORT_CHARS} 字符，请拆分后分批导入",
            "code": "PAYLOAD_TOO_LARGE",
        }

    lines, removed = strip_page_furniture(cleaned.split("\n"))
    # 行号与 raw_text 必须基于同一份文本：清洗后重新拼一次，避免"预览是脏的、切的是干净的"
    cleaned = clean_text("\n".join(lines))
    lines = cleaned.split("\n")

    payload = runner.call(SEGMENT, number_lines(lines))
    draft = check_segments(payload, len(lines), config.MAX_PROBLEMS_PER_BATCH)
    if removed:
        draft.warnings.append(f"已去除 {len(removed)} 行页眉页脚")
    if not draft.ok:
        return {"error": "；".join(draft.fatal), "code": "SEGMENT_FAILED", "detail": draft.warnings}
    snapped = snap_to_headings(draft.items, lines)
    if snapped:
        draft.warnings.append(f"已把 {snapped} 道题的起点对齐到题干标题行")

    rows: List[Dict[str, Any]] = []
    for item in draft.items:
        # 题面 = 原文切片，减去"不属于题面"的行。
        # 只能整行删，且删了哪几行会写进告警——不做任何改写，原文仍是唯一来源。
        body, mapping, removed = _slice_statement(item, lines)
        statement = clean_text("\n".join(body))
        if removed:
            draft.warnings.append(f"《{item['title']}》已剔除 {removed} 行不属于题面的文字")
        # 只挡"行号切出来是空的"这一种必然错误；够不够长、像不像一道题交给模型判断
        if not statement.strip():
            draft.warnings.append(f"《{item['title']}》行号 {item['start_line']}-{item['end_line']} 切出来是空的，已跳过")
            continue
        samples = extract_samples(statement)
        if item["sample_count"] and not samples:
            draft.warnings.append(f"《{item['title']}》模型说有样例，但程序没解析出来")
        if not judge_supported(item["judge_type"]):
            draft.warnings.append(
                f"《{item['title']}》判定方式为 {item['judge_type']}，本版本不支持，生成时会被拒收"
            )
        # 分块是排版用的"拼图"：坐标要跟着删行一起重映射，否则排版会错位
        blocks = _remap_blocks(item.pop("blocks", None), mapping)
        layout: Optional[List[Dict[str, Any]]] = None
        if blocks:
            layout, inserted = normalize_layout(blocks, statement.split("\n"))
            if inserted:
                draft.warnings.append(
                    f"《{item['title']}》模型漏标 {inserted} 行，已按正文补齐（不影响文字）"
                )
        rows.append(
            {**item, "statement_md": statement, "samples": samples, "figures": [], "layout": layout}
        )

    if not rows:
        return {"error": "模型没有切出可入库的题目", "code": "SEGMENT_FAILED", "detail": draft.warnings}

    # 图归属：按页码与题目行号区间做几何匹配，不额外调模型
    if figures:
        draft.warnings.extend(attach_figures_by_page(rows, figures, page_ranges or []))

    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO import_batches(source_name, kind, raw_text, status, pdf_key, created_at)"
            " VALUES(?,?,?, 'segmented', ?, ?)",
            (str(source_name or "")[:120], kind, cleaned, str(pdf_key or "")[:32], _now()),
        )
        batch_id = int(cursor.lastrowid)
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO import_segments(batch_id, idx, title, start_line, end_line,"
                " statement_md, samples_json, constraints_json, judge_type, tolerance,"
                " multi_testcase, sample_count, confidence, note, figures_json, layout_json,"
                " status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    batch_id,
                    index,
                    row["title"],
                    row["start_line"],
                    row["end_line"],
                    row["statement_md"],
                    _json(row["samples"]),
                    _json(row["constraints"]),
                    row["judge_type"],
                    row["tolerance"],
                    1 if row["multi_testcase"] else 0,
                    len(row["samples"]),
                    row["confidence"],
                    row["note"],
                    _json(row.get("figures") or []),
                    _json(row.get("layout") or []),
                    _now(),
                ),
            )
    return {
        "ok": True,
        "batch_id": batch_id,
        "bank": banks.view(db, bank, role),
        "library_total": int(db.query_scalar(
            "SELECT COUNT(*) FROM problems WHERE bank_id=?", (int(bank["id"]),), 0
        )),
        "library_cap": banks.cap_of(bank),
        "segments": [
            {
                "idx": index,
                "title": row["title"],
                "lines": f"{row['start_line']}-{row['end_line']}",
                "chars": len(row["statement_md"]),
                "samples": len(row["samples"]),
                "figures": len(row.get("figures") or []),
                "judge_type": row["judge_type"],
                "confidence": row["confidence"],
                "note": row["note"],
            }
            for index, row in enumerate(rows, start=1)
        ],
        "warnings": draft.warnings,
    }


def _save_figures(
    db: Any, problem_id: int, figures: Sequence[Dict[str, Any]], pdf_key: str
) -> int:
    """按已算好的 bbox 从缓存 PDF 裁剪落盘，并写 `problem_figures`。

    图在切题时只拿到坐标（很小），到这一步才真正裁图：用户没勾选的题不裁，
    磁盘上不会留一堆没人引用的 PNG。
    """
    if not figures or not pdf_key:
        return 0
    cache = config.DATA_DIR / "pdf_cache" / f"{pdf_key}.pdf"
    if not cache.is_file():
        return 0
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return 0
    from ..app.pdf_figures import FigureBlock, crop_figure_png

    out_dir = config.DATA_DIR / "assets" / f"problem_{problem_id}"
    saved = 0
    try:
        with pymupdf.open(str(cache)) as doc:
            out_dir.mkdir(parents=True, exist_ok=True)
            with db.transaction() as conn:
                conn.execute("DELETE FROM problem_figures WHERE problem_id=?", (int(problem_id),))
                for ordinal, figure in enumerate(figures, start=1):
                    bbox = figure.get("bbox") or []
                    if len(bbox) != 4:
                        continue
                    block = FigureBlock(
                        id=str(figure.get("id") or f"fig_{ordinal}"),
                        page=int(figure.get("page") or 1),
                        bbox=tuple(float(value) for value in bbox),
                        kind="",
                        width=float(figure.get("width") or 0),
                        height=float(figure.get("height") or 0),
                    )
                    data = crop_figure_png(doc, block)
                    (out_dir / f"{block.id}.png").write_bytes(data)
                    conn.execute(
                        "INSERT INTO problem_figures(problem_id, label, page, bbox_json, path,"
                        " width, height, after_line, ordinal, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(problem_id),
                            block.id,
                            block.page,
                            _json([float(value) for value in bbox]),
                            f"/assets/problem_{problem_id}/{block.id}.png",
                            block.width,
                            block.height,
                            int(figure.get("after_line") or 1),
                            ordinal,
                            _now(),
                        ),
                    )
                    saved += 1
    except Exception:  # noqa: BLE001 - 裁图失败不该让整题入库失败
        return saved
    return saved


def confirm_import(
    db: Any,
    batch_id: int,
    indices: Sequence[int],
    *,
    bank_id: int = banks.DEFAULT_ID,
    role: str = "student",
    user: str = "",
) -> Dict[str, Any]:
    bank = banks.get(db, bank_id)
    if bank is None:
        return {"error": "题库不存在", "code": "NOT_FOUND"}
    # 权限在入库这一刻再判一次：切题时可能是管理员，确认时可能是另一个人；
    # 而且接口是公开的，不能只靠前端把按钮藏起来。
    if not banks.can_manage(bank, role):
        return {"error": banks.DENY_ADD, "code": "FORBIDDEN"}
    batch = db.query_one("SELECT * FROM import_batches WHERE id=?", (int(batch_id),))
    if batch is None:
        return {"error": "导入批次不存在", "code": "NOT_FOUND"}
    wanted = {int(item) for item in indices} if indices else None
    rows = db.query_all(
        "SELECT * FROM import_segments WHERE batch_id=? ORDER BY idx", (int(batch_id),)
    )
    imported: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        index = int(row["idx"])
        if wanted is not None and index not in wanted:
            continue
        if str(row["status"]) != "pending":
            skipped.append({"idx": index, "reason": f"状态为 {row['status']}"})
            continue
        statement = str(row["statement_md"])
        digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()
        # 查重按库算：同一道题可以在默认题库和永久库各存一份
        existed = db.query_one(
            "SELECT id FROM problems WHERE bank_id=? AND statement_hash=?",
            (int(bank["id"]), digest),
        )
        if existed is not None:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE import_segments SET status='duplicate', problem_id=? WHERE id=?",
                    (int(existed["id"]), int(row["id"])),
                )
            duplicates.append({"idx": index, "problem_id": int(existed["id"]), "title": row["title"]})
            continue
        now = _now()
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash, samples_json,"
                " constraints_json, judge_type, tolerance, multi_testcase, layout_json, status,"
                " dataset_version, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?, 'draft', 0, ?, ?, ?)",
                (
                    int(bank["id"]),
                    str(row["title"]),
                    statement,
                    digest,
                    str(row["samples_json"]),
                    str(row["constraints_json"]),
                    str(row["judge_type"]),
                    str(row["tolerance"]),
                    int(row["multi_testcase"]),
                    str(row["layout_json"] or "[]"),
                    str(user or ""),
                    now,
                    now,
                ),
            )
            problem_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE import_segments SET status='confirmed', problem_id=? WHERE id=?",
                (problem_id, int(row["id"])),
            )
        imported.append({"idx": index, "problem_id": problem_id, "title": row["title"]})
        # 题建好后再裁图：没勾选的题不产生文件
        try:
            figure_list = json.loads(str(row["figures_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            figure_list = []
        if isinstance(figure_list, list) and figure_list:
            count = _save_figures(db, problem_id, figure_list, str(batch["pdf_key"] or ""))
            imported[-1]["figures"] = count
            if count == 0 and batch["pdf_key"]:
                imported[-1]["figure_warning"] = "插图裁剪失败，可重新导入该 PDF"

    # 临时库每次确认入库都把数量压回上限：超出的删最早入库的，留下的就是最新一批。
    # 永久库在这一步直接返回 0：不设上限，也不自动删题。
    trimmed = 0
    try:
        trimmed = problem_purge.trim_to_cap(db, bank_id=int(bank["id"]))
    except Exception as exc:  # noqa: BLE001 - 上限清理是收尾动作，不该判入库失败
        print(f"题库上限清理失败：{exc}", flush=True)
    # 单批就超过上限时，刚入库的题也可能被清掉；别让前端去打开一道已经不在的题
    imported = [
        item
        for item in imported
        if db.query_one("SELECT 1 FROM problems WHERE id=?", (int(item["problem_id"]),))
    ]
    if imported:
        with db.transaction() as conn:
            conn.execute("UPDATE import_batches SET status='confirmed' WHERE id=?", (int(batch_id),))
    return {
        "ok": True,
        "imported": imported,
        "duplicates": duplicates,
        "skipped": skipped,
        "bank": banks.view(db, bank, role),
        "library_total": int(db.query_scalar(
            "SELECT COUNT(*) FROM problems WHERE bank_id=?", (int(bank["id"]),), 0
        )),
        "library_cap": banks.cap_of(bank),
        "library_replaced": trimmed,
    }


def batch_detail(db: Any, batch_id: int) -> Dict[str, Any]:
    batch = db.query_one("SELECT * FROM import_batches WHERE id=?", (int(batch_id),))
    if batch is None:
        raise KeyError(batch_id)
    rows = db.query_all("SELECT * FROM import_segments WHERE batch_id=? ORDER BY idx", (int(batch_id),))
    return {
        "batch_id": int(batch["id"]),
        "status": str(batch["status"]),
        "created_at": str(batch["created_at"]),
        "segments": [
            {
                "idx": int(row["idx"]),
                "title": str(row["title"]),
                "lines": f"{int(row['start_line'])}-{int(row['end_line'])}",
                "chars": len(str(row["statement_md"])),
                "samples": int(row["sample_count"]),
                "judge_type": str(row["judge_type"]),
                "confidence": str(row["confidence"]),
                "status": str(row["status"]),
                "problem_id": int(row["problem_id"]) if row["problem_id"] else None,
            }
            for row in rows
        ],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
