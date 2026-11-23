"""题面排版分块：只校验结构，不碰原文。

单独成模块是因为 `validators.py` 已经接近 800 行硬线，而这块逻辑只服务
`check_segments`：模型给"类型 + 原文绝对行号"，代码负责换算、裁剪、合并。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: 分块粒度的最低上限：块数超过 max(本值, 行数 // 3) 视为过碎
BLOCK_MIN_LIMIT = 24

#: 排版分块的类型。少而固定：多一种类型就多一条前端分支，收益不成正比。
BLOCK_KINDS: tuple[str, ...] = (
    "heading", "para", "list", "code", "sample_input", "sample_output",
)


def check_blocks(raw: Any, total: int, offset: int = 0) -> Tuple[Any, List[str]]:
    """校验模型给的分块。返回 (可用的块列表 或 None, 告警)。

    `offset` = 本题 `start_line - 1`：模型给的是**原文绝对行号**，这里换算成题面内
    行号。让模型自己减会算错（实测差 2~3 行，整题排版被退回），所以坐标换算只由
    代码做一次。

    只做结构性校验（类型、范围、顺序、重叠）；**覆盖是否完整**要等切成题面文本后
    再查（见 segment.check_layout_coverage）。

    越界的块**裁到边界**而不是整题作废：模型常把 `end_line` 多算一两行，裁掉不丢字。
    真正的矛盾（跨类型重叠、块数过碎）仍整题作废 —— 排版是"锦上添花"，宁可退回
    自动模式，也不能让错位的块把正文拼到别的小节下面。
    """
    if raw is None:
        return None, []
    if not isinstance(raw, list) or not raw:
        return None, ["排版分块不是非空数组，已退回自动排版"]

    blocks: List[Dict[str, Any]] = []
    last_end = 0
    clamped = 0
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return None, [f"排版分块第 {position} 项不是对象，已退回自动排版"]
        kind = str(item.get("kind") or "").strip()
        if kind not in BLOCK_KINDS:
            return None, [f"排版分块第 {position} 项类型 {kind!r} 不支持，已退回自动排版"]
        try:
            start = int(item.get("from")) - offset
            end = int(item.get("to")) - offset
        except (TypeError, ValueError):
            return None, [f"排版分块第 {position} 项行号不是整数，已退回自动排版"]
        if start < 1:
            start = 1
            clamped += 1
        if end > total:
            end = total
            clamped += 1
        if start > total or end < 1:
            clamped += 1
            continue  # 整块落在题面之外，丢掉不丢字
        if end < start:
            return None, [f"排版分块第 {position} 项行号 {start}-{end} 颠倒，已退回自动排版"]
        if start <= last_end:
            # 同类型的块重叠（模型常把一段话拆成首尾相接的两块）可以安全合并：
            # 内容顺序没变，只是并回一块。跨类型重叠无法判断谁该在前，直接作废。
            previous = blocks[-1] if blocks else None
            if previous is not None and previous["kind"] == kind and start > previous["from"]:
                previous["to"] = max(int(previous["to"]), end)
                last_end = previous["to"]
                continue
            return None, [f"排版分块第 {position} 项与上一块重叠或乱序，已退回自动排版"]
        blocks.append({"kind": kind, "from": start, "to": end})
        last_end = end

    if not blocks:
        return None, ["排版分块全部越界，已退回自动排版"]
    # 逐行切块（一段正文拆成几十个块）渲染出来是一堆断句，不如自动排版
    limit = max(BLOCK_MIN_LIMIT, total // 3)
    if len(blocks) > limit:
        return None, [f"排版分块过碎（{len(blocks)} 块，上限 {limit}），已退回自动排版"]
    warnings = [f"排版分块有 {clamped} 处越界，已裁到题面边界"] if clamped else []
    return blocks, warnings
