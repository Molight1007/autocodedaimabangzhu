"""v2 本地版配置：独立端口、独立数据库、独立凭证，不碰完整版与 slim v1。

隔离靠四个强制项（`apply_env` 里覆盖写）：
1. `AUTOCODE_DB` → `data/slim2.db`
2. `AUTOCODE_SETTINGS_FILE` → `data/slim2-settings.json`
3. `AUTOCODE_SCHEMA_FILE` → 本包 schema.sql
4. `AUTOCODE_SLIM_CREDENTIALS` → `data/slim2-credentials.json`（复用 slim.auth 的 scrypt 实现）

端口默认 8767（v1 是 8766），两个可以同时开，互不影响。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs" / "slim2"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

COOKIE_NAME = "ac2_sid"
SESSION_HOURS = 12
SESSION_MAX_DAYS = 7

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_IMPORT_CHARS = 200_000
MAX_PROBLEMS_PER_BATCH = 20
MAX_CODEGEN_CASES = 20
MIN_CASES = 10
MAX_INPUT_CHARS = 2_000
MAX_CODE_CHARS = 30_000
MAX_ADVERSARIAL_CASES = 8

#: 全站每天允许的模型调用次数上限；0 表示不限。
#: 防的是「谁都能开任务」时，一个人把老师充的 API 额度刷光——账单事故也是安全事故。
DAILY_LLM_CALL_CAP = 3000

JOB_TIMEOUT_MIN = 30
MAX_DATASET_JOBS = 2
MAX_ADVERSARIAL_JOBS = 2
DEFAULT_BUDGET_CALLS = 8
RUN_TIMEOUT_MS = 2_000
#: 参考解复算的单次运行上限。标程是 Python，比学生代码慢，留宽一点；
#: 超时的条目标 unverified（不用它改数据），所以宁可给足时间。
REFERENCE_TIMEOUT_MS = 5_000

LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SEC = 300
LOGIN_LOCK_SEC = 600
#: 第一次锁只锁 60 秒：同学之间知道学号，5 次错密码就能把别人锁 10 分钟属于「整人」，
#: 而不给个锁又挡不住在线猜。所以做成两档：先短锁，累计到 LOGIN_HARD_FAILS 次才长锁。
LOGIN_SOFT_LOCK_SEC = 60
LOGIN_HARD_FAILS = 10
#: 同一 IP 的失败上限。教室常在同一个出口 NAT 后面，这条只防洪，不能按人头算。
LOGIN_IP_MAX_FAILS = 200

#: 同时在处理的连接上限。超了就断开，避免慢连接把线程堆满。
MAX_CONNECTIONS = 512
#: 单个连接的读超时（秒）。到点没写完请求就断开。
HANDLER_TIMEOUT_SEC = 30


def db_path() -> Path:
    raw = str(os.environ.get("AUTOCODE_SLIM2_DB") or "").strip()
    return Path(raw).expanduser() if raw else DATA_DIR / "slim2.db"


def settings_path() -> Path:
    raw = str(os.environ.get("AUTOCODE_SLIM2_SETTINGS") or "").strip()
    return Path(raw).expanduser() if raw else DATA_DIR / "slim2-settings.json"


def credentials_path() -> Path:
    raw = str(os.environ.get("AUTOCODE_SLIM2_CREDENTIALS") or "").strip()
    return Path(raw).expanduser() if raw else DATA_DIR / "slim2-credentials.json"


def initial_password_path() -> Path:
    return credentials_path().with_name("slim2-initial-password.txt")


def apply_env() -> None:
    """在任何业务模块导入前钉死路径；覆盖写，避免继承到 v1/完整版的值。"""
    os.environ["AUTOCODE_DB"] = str(db_path())
    os.environ["AUTOCODE_SETTINGS_FILE"] = str(settings_path())
    os.environ["AUTOCODE_SCHEMA_FILE"] = str(SCHEMA_FILE)
    os.environ["AUTOCODE_SLIM_CREDENTIALS"] = str(credentials_path())
    os.environ["AUTOCODE_SLIM_INITIAL_PASSWORD"] = str(initial_password_path())
    # 复用 slim.auth 的 scrypt/会话实现，它读的是 AUTOCODE_SLIM_* 前缀
    os.environ["AUTOCODE_SLIM_USER"] = username()
    digest = password_hash()
    if digest:
        os.environ["AUTOCODE_SLIM_PASSWORD_HASH"] = digest
    else:
        os.environ.pop("AUTOCODE_SLIM_PASSWORD_HASH", None)


def host() -> str:
    return str(os.environ.get("AUTOCODE_SLIM2_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def port() -> int:
    raw = str(os.environ.get("AUTOCODE_SLIM2_PORT") or "8767").strip()
    return int(raw) if raw.isdigit() else 8767


def allowed_hosts() -> tuple[str, ...]:
    raw = str(os.environ.get("AUTOCODE_SLIM2_ALLOWED_HOSTS") or "").strip()
    if not raw:
        return ("127.0.0.1", "localhost", "::1")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def username() -> str:
    return str(os.environ.get("AUTOCODE_SLIM2_USER") or "autocode").strip() or "autocode"


def password_hash() -> str:
    return str(os.environ.get("AUTOCODE_SLIM2_PASSWORD_HASH") or "").strip()


def new_password(length: int = 16) -> str:
    """首次启动生成随机密码：去掉易混淆字符（0/O、1/l/I），便于手抄。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def daily_call_cap() -> int:
    """每日模型调用上限；环境变量里给了数字就按那个，否则用默认值。"""
    raw = str(os.environ.get("AUTOCODE_SLIM2_DAILY_CALL_CAP") or "").strip()
    return int(raw) if raw.isdigit() else DAILY_LLM_CALL_CAP
