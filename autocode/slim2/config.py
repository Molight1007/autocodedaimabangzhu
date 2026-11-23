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
#: 题库总量上限：只保留最新入库的这么多道题。
#: 每次确认导入后检查总数，超出的部分按入库顺序从最早的删起。
MAX_PROBLEMS_TOTAL = 100
MAX_CODEGEN_CASES = 20
MIN_CASES = 10
#: 题面低于这个字数、且没提输入/输出格式时，先走 D0 补全（见 `slim2/repair.py`）。
#: 补不了才拒绝：实测 158 字的残缺题面会连烧 3 次调用、写满 32768 token 后被截断。
MIN_STATEMENT_CHARS = 300
#: 补全后的题面上限：补的是格式，不是小作文。
MAX_REPAIRED_STATEMENT_CHARS = 20_000
#: 单条测试输入上限。2026-11-22 从 20 KB → 64 KB → 128 KB：答案由代码算，
#: 一道 2 万个整数的题约 120 KB，现在能整条放进一条数据里。
#: 真正的天花板不是这里，而是 D2 一次能写出的 JSON 长度（见 MAX_BATCH_INPUT_CHARS）。
MAX_INPUT_CHARS = 128_000
MAX_INPUT_LINES = 20_000
#: 单条期望输出上限（代码执行结果）。超长的多半是程序打错或死循环，丢弃该条。
MAX_OUTPUT_CHARS = 128_000
#: 单批输入总字符上限。D2 预算 131072 tokens，192 KB 数字文本约 64k tokens，
#: 加上 JSON 结构与思考过程仍能落进预算；再往上就会稳定撞截断，
#: 所以这条线是"模型写得动"的上限，不是随意的防滥用阈值。
MAX_BATCH_INPUT_CHARS = 192_000
#: 单批数据总字符上限（输入 + 输出）。执行结果可能比输入长，留到 640 KB；
#: 再大就拒绝入库，避免有人反复生成把磁盘刷满。
MAX_BATCH_CHARS = 640_000
#: D3 单次复核请求的字符上限：按 512 KB 总量算，最多 5 批。
REVIEW_MAX_CHARS = 120_000
#: 对抗数据的单条输入上限单独收紧：D4 的输出预算更紧，且它不是"入库数据"。
MAX_ADVERSARIAL_INPUT_CHARS = 4_000
MAX_CODE_CHARS = 30_000
MAX_ADVERSARIAL_CASES = 8

#: 没有题面样例时是否允许生成测试数据。
#:
#: 关（True）＝老行为：直接拒生成，理由是"没有样例就无法锚定代码答案"。
#: 开（False）＝照常生成，但只在界面与 quality 里如实标"未经样例锚定"：
#:   代码答案照样写、照样真跑，只是没有样例可证明这份代码是对的，
#:   执行结果只能与 D3 独立复核对账，不再享有"样例全过"的可信度加成。
#:
#: 2026-11-22 改成默认放开：自编题（老师手里没样例）本来就走不通。
#: 需要严格模式时设 AUTOCODE_SLIM2_REQUIRE_SAMPLES=1。
REQUIRE_SAMPLES = False

#: 全站每天允许的模型调用次数上限；0 表示不限。
#: 防的是「谁都能开任务」时，一个人把老师充的 API 额度刷光——账单事故也是安全事故。
DAILY_LLM_CALL_CAP = 3000

JOB_TIMEOUT_MIN = 30
MAX_DATASET_JOBS = 2
MAX_ADVERSARIAL_JOBS = 2
DEFAULT_BUDGET_CALLS = 10
RUN_TIMEOUT_MS = 2_000
#: 代码答案的单次运行上限。输入放宽到 64 KB（约 2 万个整数）后，
#: Python 的 O(n log n) 仍很快，O(n²) 在 2 万规模上要几十秒；超时的条目标 failed 并丢弃。
REFERENCE_TIMEOUT_MS = 30_000

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


def require_samples() -> bool:
    """严格模式：题面没有样例就不许生成测试数据（默认关，理由见 `REQUIRE_SAMPLES`）。"""
    raw = str(os.environ.get("AUTOCODE_SLIM2_REQUIRE_SAMPLES") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return REQUIRE_SAMPLES
