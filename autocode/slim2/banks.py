"""题库：默认题库 + 管理员新建的永久库。

规则（2026-11-23 用户定）：
- 默认题库（kind=temp）：所有账号可增删，最多 `MAX_PROBLEMS_TOTAL` 道，超限删最早入库的；
- 永久库（kind=permanent）：只有管理员可增删，不限数量，任何操作都不自动删题；
- 判定只在服务端做。前端把按钮置灰只是提示，绕过按钮直接发请求一样会被拒。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config

DEFAULT_ID = 1
DEFAULT_NAME = "默认题库"
KIND_TEMP = "temp"
KIND_PERMANENT = "permanent"
MAX_NAME_CHARS = 40

DENY_ADD = "永久库的题目只能由管理员添加"
DENY_DELETE = "永久库的题目只能由管理员删除"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(db: Any, bank_id: Any) -> Optional[Dict[str, Any]]:
    """按 id 取一行；不存在返回 None。"""
    try:
        key = int(bank_id)
    except (TypeError, ValueError):
        return None
    row = db.query_one(
        "SELECT id, name, kind, created_by, created_at FROM banks WHERE id=?", (key,)
    )
    return dict(row) if row is not None else None


def cap_of(bank: Optional[Dict[str, Any]]) -> int:
    """0 表示不限。永久库永远返回 0，别再拿 100 去卡它。"""
    if is_permanent(bank):
        return 0
    return int(config.MAX_PROBLEMS_TOTAL)


def is_permanent(bank: Optional[Dict[str, Any]]) -> bool:
    return bool(bank) and str(bank.get("kind") or "") == KIND_PERMANENT


def can_manage(bank: Optional[Dict[str, Any]], role: str) -> bool:
    """这个角色能不能在这个库里增删题目。"""
    if bank is None:
        return False
    return (not is_permanent(bank)) or str(role or "") == "admin"


def view(db: Any, bank: Dict[str, Any], role: str = "student") -> Dict[str, Any]:
    """界面要的形态：数量、上限、当前角色能不能改。"""
    bank_id = int(bank["id"])
    total = int(db.query_scalar("SELECT COUNT(*) FROM problems WHERE bank_id=?", (bank_id,), 0))
    return {
        "id": bank_id,
        "name": str(bank["name"]),
        "kind": str(bank["kind"]),
        "created_by": str(bank.get("created_by") or ""),
        "created_at": str(bank.get("created_at") or ""),
        "total": total,
        "cap": cap_of(bank),
        "can_manage": can_manage(bank, role),
    }


def list_banks(db: Any, role: str = "student") -> List[Dict[str, Any]]:
    rows = db.query_all(
        "SELECT id, name, kind, created_by, created_at FROM banks ORDER BY id"
    )
    return [view(db, dict(row), role) for row in rows]


def create(db: Any, name: Any, user: str = "") -> Dict[str, Any]:
    """新建永久库。名字 1~40 字，不能与已有库重名。"""
    text = str(name or "").strip()
    if not text:
        return {"error": "库名不能为空", "code": "INVALID_INPUT"}
    if len(text) > MAX_NAME_CHARS:
        return {"error": f"库名最多 {MAX_NAME_CHARS} 个字", "code": "INVALID_INPUT"}
    if text == DEFAULT_NAME:
        return {"error": f"「{DEFAULT_NAME}」是内置题库名，换一个", "code": "INVALID_INPUT"}
    existed = db.query_one("SELECT id FROM banks WHERE name=?", (text,))
    if existed is not None:
        return {"error": f"已经有叫「{text}」的题库了", "code": "INVALID_INPUT"}
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO banks(name, kind, created_by, created_at) VALUES(?,?,?,?)",
            (text, KIND_PERMANENT, str(user or ""), _now()),
        )
        bank_id = int(cursor.lastrowid)
    bank = get(db, bank_id)
    if bank is None:
        return {"error": "题库创建失败，请重试", "code": "INTERNAL"}
    return {"ok": True, "bank": view(db, bank, "admin")}
