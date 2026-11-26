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
    # 2026-11-23：任务改成排队执行，补入队时间与 worker 参数（重启后排队任务照常能开跑）
    "jobs": (
        ("user", "TEXT NOT NULL DEFAULT ''"),
        ("args_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("queued_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "llm_calls": (("user", "TEXT NOT NULL DEFAULT ''"),),
    # 排版分块：模型只给行号，前端按块拼装；空数组表示退回自动排版
    "problems": (
        ("layout_json", "TEXT NOT NULL DEFAULT '[]'"),
        # 执行规格：题面无标准输入格式时自动生成（stdin 协议 + 转换后样例），
        # 空串表示没做过转换，按题面原文处理
        ("exec_spec_json", "TEXT NOT NULL DEFAULT ''"),
        # 题面补全：原文留档 + 标记，界面要显式说明"这段是补的"，并能一键还原。
        # 不能只覆盖 statement_md，否则用户永远不知道自己看的题被动过。
        ("statement_original", "TEXT NOT NULL DEFAULT ''"),
        ("statement_repaired", "INTEGER NOT NULL DEFAULT 0"),
        # 导入人：默认题库里非管理员只能删自己导入的题，防止一个学生手滑删掉别人的。
        # 老题回填为空串，按"公共题"处理，不追溯。
        ("created_by", "TEXT NOT NULL DEFAULT ''"),
        # 难度：导入确认时规则评估（2026-11-26）；空 = 老题，启动时回填一次
        ("difficulty", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _ensure_columns(conn: Any) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone()
    return row is not None


def _columns(conn: Any, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rebuild_problems(conn: Any) -> None:
    """老库升级：`problems` 加 `bank_id`，唯一键改成（bank_id, statement_hash）。

    为什么必须整表重建：SQLite 不能直接删掉或修改 UNIQUE 约束，只能新建表再复制。
    两个 PRAGMA 是这次迁移的关键：
    - `foreign_keys=OFF`：否则 DROP 旧表会按 ON DELETE CASCADE 把子表数据一起删掉；
    - `legacy_alter_table=ON`：否则 SQLite 会把子表外键改写成指向旧表名，
      新表建好之后外键就全部悬空。
    复制完立刻跑 `foreign_key_check`，不通过就直接报错，宁可启动失败也不带着坏库跑。
    """
    old_columns = _columns(conn, "problems")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE problems RENAME TO problems_bank_legacy")
        conn.executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        new_columns = _columns(conn, "problems")
        # 只复制两边都有的列：老库可能缺后加的列（让新表默认值生效），
        # 也可能多出别的版本才有的列（slim2 用不到，不带走）。
        copy = [name for name in new_columns if name in old_columns and name != "bank_id"]
        if copy:
            conn.execute(
                f"INSERT INTO problems({', '.join(copy + ['bank_id'])})"
                f" SELECT {', '.join(copy + ['1'])} FROM problems_bank_legacy"
            )
        old_total = int(conn.execute("SELECT COUNT(*) FROM problems_bank_legacy").fetchone()[0])
        new_total = int(conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0])
        if old_total != new_total:
            raise RuntimeError(f"题库迁移复制不完整：旧表 {old_total} 行，新表 {new_total} 行")
        conn.execute("DROP TABLE problems_bank_legacy")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"题库迁移后外键检查未通过：{bad[:3]}")
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def _ensure_default_bank(conn: Any) -> None:
    """默认题库必须有一行：老库迁移后所有题都归到 id=1 这个库里。"""
    conn.execute(
        "INSERT INTO banks(id, name, kind, created_by, created_at)"
        " VALUES(1, '默认题库', 'temp', '', ?) ON CONFLICT DO NOTHING",
        (_now(),),
    )


def _backfill_difficulty(conn: Any) -> None:
    """老题回填难度：difficulty 列是后加的（2026-11-26），导入时还没这个功能。

    只处理 difficulty='' 的行，回填一次就不再动；规则评估失败就留空，
    界面对空值不显示颜色，不影响任何业务逻辑。
    """
    rows = conn.execute(
        "SELECT id, statement_md, constraints_json, judge_type"
        " FROM problems WHERE difficulty=''"
    ).fetchall()
    if not rows:
        return
    import json

    from .difficulty import estimate

    for row in rows:
        try:
            constraints = json.loads(str(row["constraints_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            constraints = []
        if not isinstance(constraints, list):
            constraints = []
        level = estimate(
            str(row["statement_md"] or ""), constraints,
            judge_type=str(row["judge_type"] or ""),
        )
        if level:
            conn.execute(
                "UPDATE problems SET difficulty=? WHERE id=?", (level, int(row["id"]))
            )


def _init(db: Database) -> None:
    conn = db.connect()
    # 老库先重建再建表：schema.sql 里的 ix_problems_bank 依赖 bank_id，
    # 顺序反了会在 ALTER 之前就报 "no such column"。
    if _table_exists(conn, "problems") and "bank_id" not in _columns(conn, "problems"):
        _rebuild_problems(conn)
    else:
        conn.executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
    _ensure_columns(conn)
    _backfill_difficulty(conn)
    _ensure_default_bank(conn)
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
