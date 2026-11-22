"""v2 数据库门面：独立建表，不走共享 `_COLUMN_MIGRATIONS`。

为什么不用 `store.db.init_schema()`：它会顺带给 `problems/hints/...` 补完整版的列，
在 v2 库里留下一堆用不到的字段。这里只执行本包的 schema.sql，语义干净。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..store.db import Database
from . import config

_LOCK = threading.Lock()
_DB: Optional[Database] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 老库补列：CREATE TABLE IF NOT EXISTS 不会改已存在的表，缺列就 ALTER。
#: 只加列不改类型，全部带常量默认值，幂等。
_COLUMN_MIGRATIONS = {
    "adversarial_cases": (("failure_reason", "TEXT NOT NULL DEFAULT ''"),),
    "import_segments": (
        ("figures_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("layout_json", "TEXT NOT NULL DEFAULT '[]'"),
    ),
    "import_batches": (("pdf_key", "TEXT NOT NULL DEFAULT ''"),),
    # 2026-11-22：Key 改成一人一份，调用与任务都要记"是谁"的，才能各算各的额度
    "jobs": (("user", "TEXT NOT NULL DEFAULT ''"),),
    "llm_calls": (("user", "TEXT NOT NULL DEFAULT ''"),),
    # 排版分块：模型只给行号，前端按块拼装；空数组表示退回自动排版
    "problems": (("layout_json", "TEXT NOT NULL DEFAULT '[]'"),),
}


def _ensure_columns(conn: Any) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _init(db: Database) -> None:
    conn = db.connect()
    conn.executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
    _ensure_columns(conn)
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)"
        " ON CONFLICT(version) DO NOTHING",
        (2, _now()),
    )


def get_db(path: Optional[Path] = None) -> Database:
    """按路径缓存；同一进程内多次调用拿到同一份 per-thread 连接池。"""
    global _DB
    target = Path(path) if path is not None else config.db_path()
    with _LOCK:
        if _DB is None or Path(_DB.path) != target:
            config.apply_env()
            db = Database(target)
            _init(db)
            _DB = db
        return _DB
