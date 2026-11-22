"""问题反馈：学生提，管理员回。

从 api.py 拆出来，理由是本文件曾把 api.py 顶过 800 行硬线；
反馈与题库/生成流程没有耦合，拆开后两边都更容易读。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

# 分页参数与时间戳沿用 api 的实现：晚绑定，避免与 api 形成循环导入。
from .api import _now, _page_limit


# ── 问题反馈（学生提，管理员回） ──────────────────────────────────────────────
FEEDBACK_KINDS = ("bug", "idea", "other")
_FEEDBACK_STATUS = ("open", "answered", "closed")
#: 一个人一天最多提 20 条：够用，又能挡住刷屏。
FEEDBACK_DAILY_LIMIT = 20


def _fb_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _fb_replies(db: Any, ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.query_all(
        f"SELECT id, feedback_id, user, role, body, created_at FROM feedback_replies"
        f" WHERE feedback_id IN ({marks}) ORDER BY id",
        tuple(ids),
    )
    bag: Dict[int, List[Dict[str, Any]]] = {int(i): [] for i in ids}
    for row in rows:
        bag[int(row["feedback_id"])].append(dict(row))
    return bag


def list_feedback(
    db: Any, user: str, role: str, *, status: str = "", offset: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """学生只看到自己提的；管理员看到全部。附带每条下面的回复。"""
    where: List[str] = []
    params: List[Any] = []
    if role != "admin":
        where.append("user=?")
        params.append(str(user or ""))
    status = str(status or "").strip()
    if status in _FEEDBACK_STATUS:
        where.append("status=?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = db.query_scalar(f"SELECT COUNT(*) FROM feedback{clause}", tuple(params), 0)
    rows = db.query_all(
        f"SELECT id, user, name, kind, title, body, page, status, created_at, updated_at"
        f" FROM feedback{clause}"
        f" ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END, id DESC"
        f" LIMIT ? OFFSET ?",
        tuple(params + [_page_limit(limit), max(0, int(offset))]),
    )
    items = [dict(row) for row in rows]
    replies = _fb_replies(db, [int(item["id"]) for item in items])
    for item in items:
        item["replies"] = replies.get(int(item["id"]), [])

    stats = {"open": 0, "answered": 0, "closed": 0}
    scope = "" if role == "admin" else " WHERE user=?"
    scope_params: tuple = () if role == "admin" else (str(user or ""),)
    for key, count in db.query_all(
        f"SELECT status, COUNT(*) AS n FROM feedback{scope} GROUP BY status", scope_params
    ):
        if str(key) in stats:
            stats[str(key)] = int(count)
    return {"ok": True, "items": items, "total": int(total), "stats": stats, "role": role}


def create_feedback(db: Any, user: str, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(payload.get("kind") or "bug").strip().lower()
    if kind not in FEEDBACK_KINDS:
        kind = "other"
    title = _fb_text(payload.get("title"), 80)
    body = _fb_text(payload.get("body"), 2000)
    page = _fb_text(payload.get("page"), 120)
    if len(title) < 4:
        return {"error": "标题太短，请至少写 4 个字", "code": "INVALID_INPUT"}
    if len(body) < 5:
        return {"error": "描述太短，请说清楚你遇到的情况", "code": "INVALID_INPUT"}

    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")
    today = db.query_scalar(
        "SELECT COUNT(*) FROM feedback WHERE user=? AND created_at>=?",
        (str(user or ""), since), 0,
    )
    if int(today) >= FEEDBACK_DAILY_LIMIT:
        return {"error": f"今天已经提了 {FEEDBACK_DAILY_LIMIT} 条，明天再提吧", "code": "TOO_MANY"}

    now = _now()
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO feedback(user,name,kind,title,body,page,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?, 'open', ?, ?)",
            (str(user or ""), str(name or ""), kind, title, body, page, now, now),
        )
        feedback_id = int(cursor.lastrowid)
    return {"ok": True, "id": feedback_id}


def reply_feedback(
    db: Any, feedback_id: int, user: str, role: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    body = _fb_text(payload.get("body"), 2000)
    if not body:
        return {"error": "回复内容不能为空", "code": "INVALID_INPUT"}
    row = db.query_one("SELECT id, user, status FROM feedback WHERE id=?", (int(feedback_id),))
    if row is None:
        return {"error": "这条反馈不存在", "code": "NOT_FOUND"}
    if role != "admin" and str(row["user"]) != str(user or ""):
        return {"error": "只能回复自己提的问题", "code": "FORBIDDEN"}
    if str(row["status"]) == "closed":
        return {"error": "这条已经标记解决了，先不要继续回复", "code": "CLOSED"}

    now = _now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO feedback_replies(feedback_id,user,role,body,created_at)"
            " VALUES(?,?,?,?,?)",
            (int(feedback_id), str(user or ""), role, body, now),
        )
        if role == "admin":
            conn.execute(
                "UPDATE feedback SET status='answered', updated_at=? WHERE id=?",
                (now, int(feedback_id)),
            )
    return {"ok": True, "id": int(feedback_id)}


def close_feedback(db: Any, feedback_id: int, user: str, role: str) -> Dict[str, Any]:
    row = db.query_one("SELECT id, user FROM feedback WHERE id=?", (int(feedback_id),))
    if row is None:
        return {"error": "这条反馈不存在", "code": "NOT_FOUND"}
    if role != "admin" and str(row["user"]) != str(user or ""):
        return {"error": "只能关闭自己提的问题", "code": "FORBIDDEN"}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE feedback SET status='closed', updated_at=? WHERE id=?",
            (_now(), int(feedback_id)),
        )
    return {"ok": True, "id": int(feedback_id)}
