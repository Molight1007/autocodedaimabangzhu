"""给参考解补算法标签：代码 → 2~8 字算法名（数据修复 / 新题补标）。

只动标签，不动代码与提示正文；命名结果要过白名单式的轻量校验，
避免模型把「这道题可以用试除法解决」整句塞进分支名。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..prompts import loader as prompts

_BAD_WORDS = ("未标注", "解法", "这道", "参考解", "方法名", "算法名")
_BAD_CHARS = "，。；：！？、（）()【】[]"


def normalize_name(raw: Any, *, max_len: int = 12) -> Optional[str]:
    name = str(raw or "").strip().strip("「」\"'“” ")
    if not name or len(name) > max_len:
        return None
    if any(word in name for word in _BAD_WORDS):
        return None
    if any(char in name for char in _BAD_CHARS):
        return None
    return name


def infer_approach_name(
    service: Any,
    problem: Dict[str, Any],
    code: str,
    l2_text: str = "",
) -> Optional[str]:
    """一次调用，返回校验后的算法名；不合格返回 None。"""
    user = (
        f"## 题面摘要\n{str(problem.get('statement_md') or '')[:1500]}\n\n"
        f"## 参考解代码（Python，已过样例）\n```python\n{code[:12000]}\n```\n\n"
        f"## 已有 L2（可交叉验证）\n{l2_text[:800]}"
    )
    result = service._chat(
        prompts.APPROACH_LABEL, user, tag="approach_label#1", max_tokens=256
    )
    payload = result.json_obj or {}
    return normalize_name(payload.get("name"))


def pick_code(rows: list) -> str:
    """优先 Python reference，其次任意 reference，最后第一份代码。"""
    for lang, role in (("python", "reference"), ("cpp", "reference"), ("c", "reference")):
        for row in rows:
            if row["lang"] == lang and row["role"] == role and row["code"]:
                return str(row["code"])
    return str(rows[0]["code"] or "") if rows else ""


def relabel_problem(db: Any, problem_id: int, name: str) -> Dict[str, int]:
    """把空白标签回填到参考解、analysis_json、active hints 三处。"""
    counts = {"solutions": 0, "analysis": 0, "hints": 0}
    with db.transaction() as conn:
        # rowcount 在部分 SQLite 包装下返回 0，用 total_changes 差值更可靠
        before = conn.total_changes
        conn.execute(
            "UPDATE reference_solutions SET approach=?"
            " WHERE problem_id=? AND role<>'brute' AND coalesce(trim(approach),'')=''",
            (name, problem_id),
        )
        conn.execute(
            "UPDATE reference_solutions SET approach='暴力枚举'"
            " WHERE problem_id=? AND role='brute' AND coalesce(trim(approach),'')=''",
            (problem_id,),
        )
        counts["solutions"] = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            "UPDATE hints SET approach=? WHERE problem_id=? AND approach='未标注方法'",
            (name, problem_id),
        )
        counts["hints"] = conn.total_changes - before
    row = db.query_one("SELECT analysis_json FROM problems WHERE id=?", (problem_id,))
    payload = {}
    if row and row["analysis_json"]:
        try:
            payload = json.loads(row["analysis_json"])
        except json.JSONDecodeError:
            payload = {}
    changed = 0
    for item in payload.get("approaches") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip() in ("", "未标注方法"):
            item["name"] = name
            changed += 1
    if changed:
        db.execute(
            "UPDATE problems SET analysis_json=?, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), _now(), problem_id),
        )
    counts["analysis"] = changed
    return counts


def _now() -> str:
    from .prep_models import utc_now

    return utc_now()
