"""SQLite 连接工厂与最小仓储底座（设计 §7）。

统一口径（写死，不允许各模块自己 `sqlite3.connect`）：
- **per-thread**：每个线程一个连接（SQLite 连接不可跨线程共享），`threading.local` 持有。
- **WAL**：读写不互斥，UI 读 + 后台写不会互相阻塞。
- **foreign_keys=ON**：SQLite 默认关闭，不开就是假约束（删题不清子表）。
- **busy_timeout=5000**：并发写最多等 5s 再报 locked。
- PRAGMA 一律在**事务外**设置（事务内切 journal_mode 会静默失败）。

R6 约束：禁 INSERT OR REPLACE、禁字符串拼 SQL。本模块只提供 `(sql, params)` 形式的
接口，`params` 必须是序列/字典；所有调用点由 `tests/test_store_db.py` 的参数化检查兜底。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

#: 结构版本：v2=test_cases.source，v3=llm_calls.prompt_hash，v4=hints.approach，
#: v5=problems.artifact_version（产物流程版本，见 `_COLUMN_MIGRATIONS` 注释），
#: v6=generation_jobs.queued_at（生成任务改成排队执行）。
#: 迁移全部幂等；版本号只用于记录升级历史，不参与业务判断。
SCHEMA_VERSION = 6
PRAGMA_BUSY_TIMEOUT_MS = 5000

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_FILE_ENV = "AUTOCODE_SCHEMA_FILE"
_DEFAULT_DB_NAME = "autocode.db"
_ENV_DB_PATH = "AUTOCODE_DB"


def schema_file() -> Path:
    """建表脚本路径；精简版用 `AUTOCODE_SCHEMA_FILE` 指向自己的子集 schema。"""
    override = os.environ.get(SCHEMA_FILE_ENV, "").strip()
    return Path(override).expanduser() if override else _SCHEMA_FILE


def project_root() -> Path:
    """仓库根目录（autocode 包的父目录）。"""
    return Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    """默认库路径：`<repo>/data/autocode.db`，可用 `AUTOCODE_DB` 覆盖（测试/隔离用）。"""
    override = os.environ.get(_ENV_DB_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    return project_root() / "data" / _DEFAULT_DB_NAME


class Database:
    """一个库文件的门面：per-thread 连接 + 参数化查询 + 事务上下文。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self._local = threading.local()
        self._lock = threading.Lock()

    # ── 连接 ────────────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        """取当前线程的连接（首次调用时建连并设 PRAGMA）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=PRAGMA_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,  # 自动提交；事务由 transaction() 显式管
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        self._local.conn = conn
        return conn

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={PRAGMA_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── 初始化 ──────────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        """建表（幂等）；成功后写 schema_migrations。"""
        schema_sql = schema_file().read_text(encoding="utf-8")
        conn = self.connect()
        conn.executescript(schema_sql)
        self._migrate_columns(conn)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION, now),
        )

    # 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里做幂等 ALTER
    _COLUMN_MIGRATIONS = {
        "reference_solutions": (
            ("approach", "TEXT"),
            ("complexity", "TEXT"),
            ("is_primary", "INTEGER NOT NULL DEFAULT 0"),
        ),
        "problems": (
            ("analysis_json", "TEXT"),
            ("data_plan_json", "TEXT"),
            # v5：产物流程版本。老库补列后为 NULL ⇒ 一律视为旧版待升级。
            ("artifact_version", "INTEGER"),
            ("algorithm_tags", "TEXT"),
        ),
        # v2：数据来源标记（用户合并进来的数据 source='user'）
        "test_cases": (
            ("source", "TEXT NOT NULL DEFAULT 'generated'"),
        ),
        # v3：prompt 版本可追溯（老库补列，不重建表；历史行留空串表示"未记录"）
        "llm_calls": (
            ("prompt_hash", "TEXT NOT NULL DEFAULT ''"),
        ),
        # v4：提示按解法分组（空串=旧数据，UI 归到“基础解法”）
        "hints": (
            ("approach", "TEXT NOT NULL DEFAULT ''"),
        ),
        # v6：生成任务排队执行，老库补入队时间（空串=当时提交即开跑）
        "generation_jobs": (
            ("queued_at", "TEXT NOT NULL DEFAULT ''"),
        ),
    }

    @staticmethod
    def ensure_columns(
        conn: sqlite3.Connection, table: str, columns: Sequence[tuple[str, str]]
    ) -> List[str]:
        """幂等补列：只 ADD 缺失列，绝不重建表/删数据。返回实际新增的列名。

        契约 §1 铁律 3：新表走 schema.sql，列变更一律走本函数，老库可直接升级。
        """
        existing = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            # 精简版 schema 不含 hints 等表；补列只针对"表存在"的库，绝不在空库上 ALTER。
            return []
        added: List[str] = []
        for name, ddl in columns:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(name)
        return added

    @classmethod
    def _migrate_columns(cls, conn: sqlite3.Connection) -> List[str]:
        added: List[str] = []
        for table, columns in cls._COLUMN_MIGRATIONS.items():
            for name in cls.ensure_columns(conn, table, columns):
                added.append(f"{table}.{name}")
        return added

    # ── 基本操作（只收参数化 SQL）────────────────────────────────────────────
    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(
        self, sql: str, rows: Iterable[Sequence[Any]]
    ) -> sqlite3.Cursor:
        return self.connect().executemany(sql, [tuple(row) for row in rows])

    def query_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> List[sqlite3.Row]:
        return list(self.connect().execute(sql, params).fetchall())

    def query_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> Optional[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchone()

    def query_scalar(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default: Any = None
    ) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None else default

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务（BEGIN IMMEDIATE 抢写锁，避免升级死锁）。"""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ── 观测 ────────────────────────────────────────────────────────────────
    def table_counts(self, tables: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """各表行数快照（验收"重跑不增行"用）。"""
        if tables is None:
            tables = [
                str(row[0])
                for row in self.query_all(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        counts: Dict[str, int] = {}
        for table in tables:
            name = _safe_identifier(table)
            counts[name] = int(self.query_scalar(f"SELECT COUNT(*) FROM {name}", (), 0))
        return counts


_DEFAULT_DB: Optional[Database] = None
_DEFAULT_LOCK = threading.Lock()

_IDENTIFIER_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _safe_identifier(raw: Any) -> str:
    """表名/列名只允许 [A-Za-z0-9_]，用于极少数必须拼标识符的位置（值永远参数化）。"""
    name = str(raw)
    if not name or not set(name) <= _IDENTIFIER_OK or name[0].isdigit():
        raise ValueError(f"非法标识符 {raw!r}（只允许字母数字下划线）")
    return name


def get_db(path: Optional[Path] = None, *, init: bool = True) -> Database:
    """进程级默认库（可传 path 建独立实例，测试互不干扰）。"""
    global _DEFAULT_DB
    if path is not None:
        db = Database(path)
        if init:
            db.init_schema()
        return db
    with _DEFAULT_LOCK:
        if _DEFAULT_DB is None:
            _DEFAULT_DB = Database(default_db_path())
        db = _DEFAULT_DB
    if init:
        db.init_schema()
    return db


def reset_default_db() -> None:
    """测试用：丢掉进程级默认库句柄。"""
    global _DEFAULT_DB
    with _DEFAULT_LOCK:
        if _DEFAULT_DB is not None:
            _DEFAULT_DB.close()
        _DEFAULT_DB = None
