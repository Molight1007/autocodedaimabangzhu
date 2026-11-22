"""账号门禁：scrypt 密码、会话表、登录限流。

两类账号并存：
- 管理员：一组凭证，来自环境变量或 `data/slim2-credentials.json`，用于运维与兜底。
- 学生：`student_accounts` 表，账号=学号，175 人一人一号。
两者权限相同（同一套接口），区别只在会话里记的 `user` 不同。

设计取舍（有意保持简单）：
- 会话只存 token 的 SHA256，Cookie 里的明文即使泄漏也无法反查库。
- 限流放内存：单进程部署，重启清零可接受；不引入 Redis。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


# ── 时间 ────────────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── 密码 ────────────────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`，可直接写进环境变量。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        str(password).encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """格式不合法一律 False；比较用常量时间。"""
    parts = str(stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
        digest = hashlib.scrypt(
            str(password).encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# ── 凭证引导 ────────────────────────────────────────────────────────────────
def _credentials_path() -> Path:
    return config.credentials_path()


def _initial_password_path() -> Path:
    raw = str(os.environ.get("AUTOCODE_SLIM_INITIAL_PASSWORD") or "").strip()
    return Path(raw).expanduser() if raw else config.initial_password_path()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_credentials() -> Tuple[str, str, Optional[str]]:
    """返回 `(user, password_hash, fresh_password)`；`fresh_password` 只在首次生成时有值。"""
    user = config.username()
    env_hash = config.password_hash()
    if env_hash:
        return user, env_hash, None

    path = _credentials_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stored_user = str(data.get("user") or user)
            stored_hash = str(data.get("password_hash") or "")
            if stored_hash:
                return stored_user, stored_hash, None
        except (OSError, json.JSONDecodeError):
            pass

    plain = config.new_password()
    digest = hash_password(plain)
    _write_private(
        path,
        json.dumps(
            {"user": user, "password_hash": digest, "created_at": iso(utcnow())},
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_private(
        _initial_password_path(),
        f"用户：{user}\n密码：{plain}\n生成时间：{iso(utcnow())}\n"
        "首次登录后请删除本文件；改密用 python -m autocode.slim2.passwd\n",
    )
    return user, digest, plain


def update_password(new_password: str) -> str:
    """运维改密：写回凭证文件（环境变量优先时不会被覆盖）。"""
    user = config.username()
    digest = hash_password(new_password)
    _write_private(
        _credentials_path(),
        json.dumps(
            {"user": user, "password_hash": digest, "created_at": iso(utcnow())},
            ensure_ascii=False,
            indent=2,
        ),
    )
    return digest


# ── 学生账号 ────────────────────────────────────────────────────────────────
def find_student(db: Any, student_no: str) -> Optional[Dict[str, Any]]:
    """按学号取一行；没有就 None。"""
    key = str(student_no or "").strip()
    if not key:
        return None
    row = db.query_one(
        "SELECT student_no, name, class_name, password_hash, status"
        " FROM student_accounts WHERE student_no=?",
        (key,),
    )
    return dict(row) if row is not None else None


def authenticate(db: Any, username: str, password: str) -> Optional[Dict[str, Any]]:
    """先查学生表，再回落管理员凭证。成功返回身份字典，失败返回 None。

    返回值：`{user, name, role}`，`role` 为 `student` / `admin`。
    两类账号权限相同，`role` 只用于展示与后续审计。
    """
    user = str(username or "").strip()
    secret = str(password or "")
    if not user or not secret:
        return None

    student = find_student(db, user)
    if student is not None:
        if str(student.get("status") or "") != "active":
            return None
        if not verify_password(secret, str(student.get("password_hash") or "")):
            return None
        return {"user": user, "name": str(student.get("name") or ""), "role": "student"}

    admin_user, stored, _fresh = load_credentials()
    if hmac.compare_digest(user.encode("utf-8"), admin_user.encode("utf-8")) and verify_password(
        secret, stored
    ):
        return {"user": admin_user, "name": admin_user, "role": "admin"}
    return None


def touch_login(db: Any, student_no: str) -> None:
    """记一次成功登录；失败不写，避免把审计写成噪声。"""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE student_accounts SET last_login_at=? WHERE student_no=?",
            (iso(utcnow()), str(student_no or "")),
        )


def role_of(user: str) -> str:
    """管理员 = 服务器上配置的那个运维账号（`AUTOCODE_SLIM2_USER`），其余都是学生。"""
    target = str(user or "")
    admin = config.username()
    if target and hmac.compare_digest(target.encode("utf-8"), admin.encode("utf-8")):
        return "admin"
    return "student"


def display_name(db: Any, user: str) -> str:
    """会话里只有登录名；显示名字时学生查花名册，查不到就用登录名。"""
    row = db.query_one("SELECT name FROM student_accounts WHERE student_no=?", (str(user or ""),))
    if row is not None and str(row["name"] or "").strip():
        return str(row["name"])
    return str(user or "")


def audit(db: Any, user: str, action: str, target: str = "", detail: str = "") -> None:
    """记一条操作审计。写失败不许影响主流程——审计是旁路，不是业务。"""
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(at,user,action,target,detail) VALUES(?,?,?,?,?)",
                (iso(utcnow()), str(user or ""), str(action or ""), str(target or ""), str(detail or "")),
            )
    except Exception:  # noqa: BLE001 - 审计失败不阻塞用户操作
        pass


# ── 会话 ────────────────────────────────────────────────────────────────────
def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_session(db: Any, user: str, ip: str) -> Tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + timedelta(hours=config.SESSION_HOURS)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user, ip, created_at, last_seen, expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (_token_hash(token), user, ip, iso(now), iso(now), iso(expires)),
        )
    return token, expires


def load_session(db: Any, token: str) -> Optional[Dict[str, Any]]:
    """校验 + 滑动续期。返回会话行；无效返回 None 并顺手清理。"""
    if not token:
        return None
    key = _token_hash(token)
    row = db.query_one("SELECT * FROM sessions WHERE token_hash=?", (key,))
    if row is None:
        return None
    now = utcnow()
    expires = parse_iso(row["expires_at"])
    created = parse_iso(row["created_at"])
    absolute_deadline = (created or now) + timedelta(days=config.SESSION_MAX_DAYS)
    if expires is None or expires <= now or absolute_deadline <= now:
        delete_session(db, token)
        return None
    new_expires = min(now + timedelta(hours=config.SESSION_HOURS), absolute_deadline)
    last_seen = parse_iso(row["last_seen"])
    # 前端 2s 轮询会反复打这个接口；last_seen 每分钟落一次库即可，避免无谓的写锁。
    if last_seen is None or (now - last_seen).total_seconds() >= 60:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
                (iso(now), iso(new_expires), key),
            )
    return {
        "user": row["user"],
        "created_at": row["created_at"],
        "expires_at": iso(new_expires),
    }


def delete_session(db: Any, token: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def delete_all_sessions(db: Any) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions")


def purge_expired(db: Any) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(utcnow()),))


# ── 登录限流 ────────────────────────────────────────────────────────────────
class LoginLimiter:
    """登录限流：账号维度锁人，IP 维度防洪。

    为什么不按 IP 算人头：学生大多在同一个校园出口 NAT 后面，上百人共用一个源 IP。
    按 IP 卡 5 次会让「第一个打错密码的人锁住全班」。所以——
    - 账号：失败 5 次先锁 60 秒，累计 10 次才锁 10 分钟，只锁这一个学号；
      这样别人没法用 5 次错密码把你锁一节课，在线猜密码又仍然被按住；
    - IP：窗口内失败 `ip_max` 次才锁，只当作防洪阀，不当作按人配额（默认 200）；
    - 不做全站计数：开学第一天集中登录，任何全站阈值都会误伤一片。
    """

    def __init__(
        self,
        max_fails: int = config.LOGIN_MAX_FAILS,
        window_sec: int = config.LOGIN_WINDOW_SEC,
        lock_sec: int = config.LOGIN_LOCK_SEC,
        ip_max: int = config.LOGIN_IP_MAX_FAILS,
        soft_lock_sec: int = config.LOGIN_SOFT_LOCK_SEC,
        hard_fails: int = config.LOGIN_HARD_FAILS,
    ) -> None:
        self.max_fails = max(1, int(max_fails))
        self.window_sec = max(1, int(window_sec))
        self.lock_sec = max(1, int(lock_sec))
        self.ip_max = max(self.max_fails, int(ip_max))
        self.soft_lock_sec = max(1, min(int(soft_lock_sec), self.lock_sec))
        self.hard_fails = max(self.max_fails + 1, int(hard_fails))
        self._user_fails: Dict[str, deque] = defaultdict(deque)
        self._user_locked: Dict[str, datetime] = {}
        self._ip_fails: Dict[str, deque] = defaultdict(deque)
        self._ip_locked: Dict[str, datetime] = {}
        self._lock = threading.Lock()

    def _prune(self, moment: datetime) -> None:
        edge = moment - timedelta(seconds=self.window_sec)
        for bucket in (self._user_fails, self._ip_fails):
            for key in list(bucket):
                queue = bucket[key]
                while queue and queue[0] < edge:
                    queue.popleft()
                if not queue:
                    bucket.pop(key, None)
        for locked in (self._user_locked, self._ip_locked):
            for key in list(locked):
                if locked[key] <= moment:
                    locked.pop(key, None)

    @staticmethod
    def _remain(until: Optional[datetime], moment: datetime) -> int:
        return max(1, int((until - moment).total_seconds())) if until else 0

    def retry_after(self, ip: str, user: str = "") -> int:
        moment = utcnow()
        with self._lock:
            self._prune(moment)
            wait = self._remain(self._ip_locked.get(ip), moment)
            if wait:
                return wait
            return self._remain(self._user_locked.get(str(user or "")), moment)

    def record_failure(self, ip: str, user: str = "") -> int:
        """记一次失败，返回锁定时长（0 表示尚未锁定）。账号与 IP 各自计数。"""
        moment = utcnow()
        key = str(user or "")
        with self._lock:
            self._prune(moment)
            wait = 0
            self._ip_fails[ip].append(moment)
            if len(self._ip_fails[ip]) >= self.ip_max:
                self._ip_locked[ip] = moment + timedelta(seconds=self.lock_sec)
                self._ip_fails.pop(ip, None)
                wait = self.lock_sec
            self._user_fails[key].append(moment)
            tries = len(self._user_fails[key])
            if tries >= self.hard_fails:
                self._user_locked[key] = moment + timedelta(seconds=self.lock_sec)
                wait = max(wait, self.lock_sec)
            elif tries >= self.max_fails:
                self._user_locked[key] = moment + timedelta(seconds=self.soft_lock_sec)
                wait = max(wait, self.soft_lock_sec)
            return wait

    def reset(self, ip: str, user: str = "") -> None:
        """登录成功后清掉这个账号与这个 IP 的失败计数。"""
        key = str(user or "")
        with self._lock:
            self._user_fails.pop(key, None)
            self._user_locked.pop(key, None)
            self._ip_fails.pop(ip, None)
            self._ip_locked.pop(ip, None)


def active_sessions(db: Any) -> List[Dict[str, Any]]:
    rows = db.query_all(
        "SELECT user, ip, created_at, last_seen, expires_at FROM sessions ORDER BY created_at DESC"
    )
    return [dict(row) for row in rows]
