"""解法可信度审计：模型自称的「复杂度」必须与代码结构对得上。

背景（2026-11-18 实测 #26 仓库规划）：
- 模型把主解改个变量名交上来冒充「贪心」；
- 修掉查重时机后又出现「动态规划 O(n m log n)」——回读代码发现它与主解
  **逐行相同**（只少一行注释），AST 结构查重 `same_structure` 明明返回 True，
  却因为 `build_all` 里按 `verified` 过滤（那时还没置位）而从未生效；
- 曾试过用「最大用例实测耗时」判"声称更优是否变快"，但两份近乎相同的代码
  测出 351ms vs 928ms，进程噪声盖过真实差异，给出假阴性 —— **计时判据已废弃**。

本模块改为**确定性**判据，不跑代码、不联网、无噪声：
  对「声称比主解更低阶」的备选解，数它 AST 里最深 for/while 嵌套层数。
  若嵌套层数没有下降，说明标签与代码不符（典型：三重循环标 O(n log n)），剔除。

边界（诚实口径）：
- 循环嵌套 ≠ 渐进复杂度，这里只做"明确不符才剔除"的保守反证，不做形式化证明；
- 没声称更优、复杂度估不出来、代码解析不了的一律不动，避免误杀；
- 主解必须是与备选解同语言的参考解（cpp 代码 ast.parse 不了，不能当基准）。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Tuple


def claimed_degree(complexity: str) -> Optional[int]:
    """从复杂度声明里估出 n 的最高次幂；估不出返回 None（不参与审计）。

    "O(n m log n)" → 1，"O(n^2 m)" → 2，"O(n^3)" → 3。
    估不出来的（如 "未声明"、"O(1)"、空串）不参与审计，避免误杀。
    """
    text = str(complexity or "").lower().replace("²", "^2").replace("³", "^3")
    compact = text.replace(" ", "")
    if "o(" not in compact:
        return None
    if re.search(r"n\s*\^\s*3|n\s*\*\s*n\s*\*\s*n|n³", text):
        return 3
    if re.search(r"n\s*\^\s*2|n\s*\*\s*n|n²", text):
        return 2
    if "n" in text:
        return 1
    return None


def loop_depth(code: str) -> Optional[int]:
    """AST 里最深的 for/while 嵌套层数；解析不了返回 None（保守跳过）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    best = 0

    def visit(node: ast.AST, depth: int) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.For, ast.While, ast.AsyncFor)):
                best = max(best, depth + 1)
                visit(child, depth + 1)
            else:
                visit(child, depth)

    visit(tree, 0)
    return best


def audit_claims(service: Any, refs: Dict[str, Any]) -> List[str]:
    """剔除「声称更低阶、循环嵌套却没降」的备选解；返回被剔除的 key 列表。

    就地修改 `refs`（调用方随后用 save_references 同步删库），不新增 LLM 调用。
    """
    alt_pool: List[Tuple[str, Any]] = [
        (key, artifact)
        for key, artifact in refs.items()
        if str(getattr(artifact, "role", "")).startswith("alt")
        and claimed_degree(getattr(artifact, "complexity", "")) is not None
    ]
    if not alt_pool:
        return []
    lang = str(getattr(alt_pool[0][1], "lang", ""))
    same_lang = [
        artifact
        for artifact in refs.values()
        if str(getattr(artifact, "lang", "")) == lang
        and str(getattr(artifact, "role", "")) == "reference"
    ]
    if not same_lang:
        service._note("复杂度声明审计跳过：没有与备选解同语言的参考解")
        return []
    primary = next(
        (artifact for artifact in same_lang if getattr(artifact, "is_primary", 0)),
        same_lang[0],
    )
    base_degree = claimed_degree(getattr(primary, "complexity", ""))
    base_depth = loop_depth(getattr(primary, "code", ""))
    if base_degree is None or base_depth is None:
        return []
    candidates = [
        (key, artifact)
        for key, artifact in alt_pool
        if claimed_degree(getattr(artifact, "complexity", "")) < base_degree
    ]
    if not candidates:
        return []

    dropped: List[str] = []
    for key, artifact in candidates:
        depth = loop_depth(getattr(artifact, "code", ""))
        if depth is None:
            continue  # 解析不了就不下结论，保守保留
        name = getattr(artifact, "approach", "")
        claim = getattr(artifact, "complexity", "")
        if depth >= base_depth:
            dropped.append(key)
            service._note(
                f"备选解「{name}」声称 {claim}，但代码仍有 {depth} 层循环嵌套"
                f"（主解 {base_depth} 层），复杂度声明与代码不符，已剔除"
            )
            refs.pop(key, None)
        else:
            service._note(
                f"备选解「{name}」循环嵌套降至 {depth} 层（主解 {base_depth} 层），保留"
            )
    return dropped
