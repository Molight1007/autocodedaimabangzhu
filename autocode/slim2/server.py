"""HTTP 入口：白名单路由 + 账号门禁（管理员 + 学生学号）。

路由表是唯一真源：没有注册的路径一律 404，不靠"分支里忘了删"来兜底。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import auth
from . import api, config, jobs, user_llm
from .db import get_db
from .llm_stage import StageError, StageRunner

_DB: Any = None
_LIMITER = auth.LoginLimiter()
_CODE_STATUS = {
    "AUTH_REQUIRED": 401,
    "AUTH_FAILED": 401,
    "RATE_LIMITED": 429,
    "INVALID_INPUT": 400,
    "PAYLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "FORBIDDEN_HOST": 403,
    "FORBIDDEN_ORIGIN": 403,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "DUPLICATE_PROBLEM": 409,
    "JOB_RUNNING": 409,
    "NEED_SAMPLES": 409,
    "NO_DATA": 409,
    "FORBIDDEN_DELETE": 409,
    "CONFIRM_REQUIRED": 409,
    "BUDGET_EXHAUSTED": 429,
    "SERVICE_BUSY": 429,
    "SEGMENT_FAILED": 502,
    "LLM_FAILED": 502,
    "INTERNAL": 500,
    # 文件导入：体积/格式类错误给准确状态，前端提示才分得清"文件太大"还是"解析不了"
    "TOO_LARGE": 413,
    "BAD_FILE": 415,
    "BAD_PDF": 422,
    "BAD_DOCX": 422,
    "EMPTY_PDF": 422,
    "EMPTY_DOCX": 422,
    "ENCRYPTED_PDF": 422,
    "BINARY_FILE": 415,
    "TOO_SHORT": 422,
    "NO_PDF_ENGINE": 501,
    "NO_DOCX_ENGINE": 501,
    "NO_KEY": 400,
    "LLM_UNREACHABLE": 502,
}


def _db() -> Any:
    global _DB
    if _DB is None:
        _DB = get_db()
    return _DB


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    """限流分桶用的客户端 IP。

    取 X-Forwarded-For 的**最后一段**，不是第一段：Caddy/nginx 默认把真实对端
    **追加**到已有头部末尾，客户端能自己伪造前面的段。取第一段等于让攻击者
    每次换个假 IP 就绕开限流；取最后一段才是可信代理写进去的那个地址。
    （要求前面只有一层自己可控的代理；多层 CDN 场景需改成按 trusted_proxies 截断。）
    """
    if str(os.environ.get("AUTOCODE_SLIM2_TRUST_PROXY") or "") == "1":
        chain = [
            item.strip()
            for item in str(handler.headers.get("X-Forwarded-For") or "").split(",")
            if item.strip()
        ]
        if chain:
            return chain[-1]
    return str(handler.client_address[0] if handler.client_address else "")


def _split_host(raw: Optional[str]) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 0 else text
    return text.rsplit(":", 1)[0] if ":" in text else text


def _is_secure(handler: BaseHTTPRequestHandler) -> bool:
    return str(handler.headers.get("X-Forwarded-Proto") or "").lower() == "https"


def _host_ok(handler: BaseHTTPRequestHandler) -> bool:
    return _split_host(handler.headers.get("Host")) in config.allowed_hosts()


def _origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    origin = str(handler.headers.get("Origin") or "").strip().rstrip("/")
    if not origin:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.netloc.lower() == str(handler.headers.get("Host") or "").lower()


def _json_ok(handler: BaseHTTPRequestHandler) -> bool:
    main = str(handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return main == "application/json"


def _cookie_token(handler: BaseHTTPRequestHandler) -> str:
    for part in str(handler.headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == config.COOKIE_NAME:
            return value.strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "autoCodeData/2.0"
    protocol_version = "HTTP/1.1"
    #: 空闲连接多久断开。HTTP/1.1 是长连接，没有这个上限时浏览器挂着的空 socket
    #: 会一直占一个线程；175 个学生同时在线能把线程数堆到上千。
    timeout = config.HANDLER_TIMEOUT_SEC

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, status: int, code: str, message: str, **extra: Any) -> None:
        payload = {"error": message, "code": code}
        payload.update(extra)
        self._json(payload, status)

    def _result(self, payload: Any) -> None:
        status = 200
        if isinstance(payload, dict) and payload.get("error"):
            status = _CODE_STATUS.get(str(payload.get("code") or ""), 400)
        self._json(payload, status)

    def _html(self, name: str, status: int = 200) -> None:
        """页面走条件请求：app.html 有 148 KB，175 人一起上课时每次刷新都全量传
        会顶满带宽。带 ETag 后浏览器只回一个 304（几百字节），内容变了才重传。"""
        path = config.STATIC_DIR / name
        if not path.is_file():
            return self._fail(500, "INTERNAL", f"缺少页面 {name}")
        raw = path.read_bytes()
        etag = f'"{hashlib.sha256(raw).hexdigest()[:16]}"'
        if str(self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session(self) -> Optional[Dict[str, Any]]:
        token = _cookie_token(self)
        return auth.load_session(_db(), token) if token else None

    def _audit(self, action: str, target: str = "", detail: str = "") -> None:
        """记一条操作审计；session 里是谁就记谁。"""
        session = self._session() or {}
        auth.audit(_db(), str(session.get("user") or ""), action, target, detail)

    def _user(self) -> str:
        session = self._session() or {}
        return str(session.get("user") or "")

    def _cap_left(self, user: str = "") -> bool:
        """这个账号今天还能不能调模型。按人算：一个人刷爆不影响别人。"""
        cap = config.daily_call_cap()
        if cap <= 0:
            return True
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        row = _db().query_one(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE created_at >= ? AND user=?",
            (since, str(user or "")),
        )
        return int(row["n"] if row is not None else 0) < cap

    def _cap_exhausted(self) -> None:
        self._fail(
            429,
            "DAILY_CAP",
            f"你今天的模型调用额度已用完（每人每天 {config.daily_call_cap()} 次），请明天再试",
        )

    def _llm_config(self):
        """当前登录账号自己的模型凭据；没配返回 None。"""
        return user_llm.config_for(_db(), self._user())

    def _no_key(self) -> None:
        self._fail(
            409,
            "NO_LLM_KEY",
            "请先在右上角「模型设置」里填写你自己的 API Key；"
            "每个账号各用各的 Key，互不通用",
        )

    def _read_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._fail(400, "INVALID_INPUT", "Content-Length 非法")
            return None
        if length < 0 or length > config.MAX_BODY_BYTES:
            self._fail(413, "PAYLOAD_TOO_LARGE", "请求体过大")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._fail(400, "INVALID_INPUT", "请求体不是合法 JSON")
            return None
        if not isinstance(payload, dict):
            self._fail(400, "INVALID_INPUT", "请求体必须是 JSON 对象")
            return None
        return payload

    # ── GET ────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        # 浏览器在慢请求上会主动断开（切页签、刷新），写响应时抛 ConnectionResetError。
        # 这类断连不是错误，但默认会在 stderr 打整段 traceback，淹没真正的异常。
        try:
            self._do_get()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _do_get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if not _host_ok(self):
            return self._fail(403, "FORBIDDEN_HOST", "Host 不在白名单")
        try:
            if path == "/healthz":
                return self._json({"ok": True})
            if path == "/favicon.ico":
                # 没有图标就明确说没有；否则浏览器这次请求会落到需要登录的 API 分支，
                # 控制台里留一条 401 假报错，排查真问题时干扰很大。
                return self._send(204, b"", "text/plain; charset=utf-8")
            if path == "/login":
                if self._session():
                    return self._redirect("/")
                return self._html("login.html")
            if path == "/":
                if not self._session():
                    return self._redirect("/login")
                return self._html("app.html")
            if path == "/api/session":
                session = self._session()
                user = str((session or {}).get("user", ""))
                return self._json({
                    "authenticated": bool(session),
                    "user": user,
                    "role": auth.role_of(user) if session else "",
                    "name": auth.display_name(_db(), user) if session else "",
                    "expires_at": (session or {}).get("expires_at", ""),
                })
            if path.startswith("/assets/"):
                if not self._session():
                    return self._fail(401, "AUTH_REQUIRED", "请先登录")
                return self._serve_asset(path)
            return self._api_get(path, parse_qs(parsed.query))
        except ValueError:
            return self._fail(400, "INVALID_INPUT", "ID 必须是整数")
        except KeyError:
            return self._fail(404, "NOT_FOUND", "资源不存在")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._fail(500, "INTERNAL", f"服务内部错误（{secrets.token_hex(4)}）")

    def _serve_asset(self, path: str) -> None:
        """题目插图：只读 `data/assets` 下的图片，`is_relative_to` 防目录穿越。"""
        root = (config.DATA_DIR / "assets").resolve()
        # urlsplit 不解码百分号转义，直接拼会找不到文件
        target = (root / unquote(path[len("/assets/") :])).resolve()
        if not target.is_relative_to(root):
            return self._fail(403, "FORBIDDEN", "路径非法")
        if not target.is_file():
            return self._fail(404, "NOT_FOUND", "图片不存在")
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(target.suffix.lower(), "application/octet-stream")
        # 插图按内容哈希做条件请求：同一道题来回切时不用重复下载几百 KB 的图
        raw = target.read_bytes()
        etag = f'"{hashlib.sha256(raw).hexdigest()[:16]}"'
        if str(self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(raw)

    def _api_get(self, path: str, query: Dict[str, list]) -> None:
        if not self._session():
            return self._fail(401, "AUTH_REQUIRED", "请先登录")
        user = self._user()
        role = auth.role_of(user)
        if path == "/api/banks":
            return self._json(api.list_banks(_db(), role))
        if path == "/api/problems":
            return self._json(api.list_problems(
                _db(),
                q=str((query.get("q") or [""])[0]),
                status=str((query.get("status") or [""])[0]),
                offset=_int(query, "offset", 0),
                limit=_int(query, "limit", 50),
                bank_id=_int(query, "bank_id", 1),
                role=role,
            ))
        if path == "/api/settings":
            return self._json(api.llm_settings(_db(), self._user()))
        if path == "/api/feedback":
            user = self._user()
            return self._json(api.list_feedback(
                _db(), user, auth.role_of(user),
                status=str((query.get("status") or [""])[0]),
                offset=_int(query, "offset", 0),
                limit=_int(query, "limit", 50),
            ))
        parts = [item for item in path.split("/") if item]
        if len(parts) == 3 and parts[:2] == ["api", "import"]:
            return self._json(api.import_detail(_db(), int(parts[2])))
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            return self._json(api.job_status(_db(), int(parts[2])))
        if len(parts) >= 3 and parts[:2] == ["api", "problems"]:
            problem_id = int(parts[2])
            if len(parts) == 3:
                return self._json(api.problem_detail(_db(), problem_id, role))
            if len(parts) == 4 and parts[3] == "cases":
                return self._json(api.list_cases(
                    _db(), problem_id,
                    layer=str((query.get("layer") or [""])[0]),
                    offset=_int(query, "offset", 0),
                    limit=_int(query, "limit", 50),
                ))
            if len(parts) == 4 and parts[3] == "adversarial":
                return self._json(api.list_adversarial(_db(), problem_id))
            if len(parts) == 4 and parts[3] == "export":
                fmt = str((query.get("format") or ["json"])[0]).lower()
                result = api.export_problem(_db(), problem_id, fmt)
                if not result.get("ok"):
                    code = str(result.get("code") or "EXPORT_FAILED")
                    status = 404 if code == "NOT_FOUND" else 409
                    return self._fail(status, code, str(result.get("error") or "导出失败"))
                # 附件名只放 ASCII：中文文件名需要 RFC 5987 编码，浏览器支持参差
                return self._send(
                    200,
                    str(result["content"]).encode("utf-8"),
                    str(result["content_type"]),
                    {"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
                )
        return self._fail(404, "NOT_FOUND", "接口不存在")

    # ── POST ───────────────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _do_post(self) -> None:
        path = urlsplit(self.path).path
        if not _host_ok(self):
            return self._fail(403, "FORBIDDEN_HOST", "Host 不在白名单")
        if not _json_ok(self):
            return self._fail(415, "UNSUPPORTED_MEDIA_TYPE", "POST 必须是 application/json")
        if not _origin_ok(self):
            return self._fail(403, "FORBIDDEN_ORIGIN", "Origin 不允许")
        payload = self._read_body()
        if payload is None:
            return
        try:
            if path == "/api/login":
                return self._login(payload)
            if path == "/api/logout":
                return self._logout()
            return self._api_post(path, payload)
        except ValueError:
            return self._fail(400, "INVALID_INPUT", "ID 必须是整数")
        except KeyError:
            return self._fail(404, "NOT_FOUND", "资源不存在")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._fail(500, "INTERNAL", f"服务内部错误（{secrets.token_hex(4)}）")

    def _login(self, payload: Dict[str, Any]) -> None:
        ip = _client_ip(self)
        supplied_user = str(payload.get("username") or "").strip()
        supplied_password = str(payload.get("password") or "")
        wait = _LIMITER.retry_after(ip, supplied_user)
        if wait > 0:
            return self._fail(429, "RATE_LIMITED", "尝试过于频繁", retry_after=wait)
        actor = auth.authenticate(_db(), supplied_user, supplied_password)
        if actor is None:
            lock = _LIMITER.record_failure(ip, supplied_user)
            if lock:
                return self._fail(429, "RATE_LIMITED", "尝试过于频繁", retry_after=lock)
            return self._fail(401, "AUTH_FAILED", "用户名或密码错误")
        _LIMITER.reset(ip, supplied_user)
        user = str(actor["user"])
        token, expires = auth.create_session(_db(), user, ip)
        if actor["role"] == "student":
            auth.touch_login(_db(), user)
        cookie = (
            f"{config.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax;"
            f" Max-Age={config.SESSION_HOURS * 3600}"
        )
        if _is_secure(self):
            cookie += "; Secure"
        body = json.dumps(
            {
                "ok": True,
                "user": user,
                "name": actor.get("name", ""),
                "role": actor["role"],
                "expires_at": auth.iso(expires),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8", {"Set-Cookie": cookie})

    def _logout(self) -> None:
        token = _cookie_token(self)
        if token:
            auth.delete_session(_db(), token)
        self._send(
            200, json.dumps({"ok": True}).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Set-Cookie": f"{config.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
        )

    def _api_post(self, path: str, payload: Dict[str, Any]) -> None:
        if not self._session():
            return self._fail(401, "AUTH_REQUIRED", "请先登录")
        parts = [item for item in path.split("/") if item]

        user = self._user()
        role = auth.role_of(user)
        if path == "/api/banks":
            if role != "admin":
                return self._fail(403, "FORBIDDEN", "只有管理员可以新建永久库")
            self._audit("create_bank", str(payload.get("name") or "")[:80])
            return self._result(api.create_bank(_db(), payload, role, user))
        if path == "/api/import/text":
            denied = api.bank_write_error(_db(), payload.get("bank_id"), role)
            if denied:
                return self._result(denied)
            if not self._cap_left(user):
                return self._cap_exhausted()
            cfg = user_llm.config_for(_db(), user)
            if cfg is None:
                return self._no_key()
            runner = StageRunner(_db(), budget=2, llm_config=cfg, user=user)
            try:
                return self._result(api.import_text(_db(), runner, payload, role))
            except StageError as exc:
                return self._fail(502, exc.code, str(exc))
        if path == "/api/import/extract":
            if not self._cap_left(user):
                return self._cap_exhausted()
            # 任何文件都可能要用模型（图片、扫描版 PDF），所以有 Key 就把 runner 备好；
            # 没有 Key 也照常往下走——纯文本/Word/带文字层的 PDF 本来就不需要它。
            # 不按后缀预判：后缀会骗人（截图存成 .dat、docx 改名 .doc），
            # 真正的分派在 api 层按文件内容做。runner 建出来不调用就不花钱。
            cfg = user_llm.config_for(_db(), user)
            runner = StageRunner(_db(), budget=6, llm_config=cfg, user=user) if cfg is not None else None
            try:
                return self._result(api.import_extract(_db(), payload, runner))
            except StageError as exc:
                return self._fail(502, exc.code, str(exc))
        if path == "/api/import/file":
            denied = api.bank_write_error(_db(), payload.get("bank_id"), role)
            if denied:
                return self._result(denied)
            if not self._cap_left(user):
                return self._cap_exhausted()
            cfg = user_llm.config_for(_db(), user)
            if cfg is None:
                return self._no_key()
            runner = StageRunner(_db(), budget=2, llm_config=cfg, user=user)
            try:
                return self._result(api.import_file(_db(), runner, payload, role))
            except StageError as exc:
                return self._fail(502, exc.code, str(exc))
        if path == "/api/settings":
            return self._result(api.save_llm_settings(_db(), payload, user))
        if path == "/api/feedback":
            return self._result(api.create_feedback(
                _db(), user, auth.display_name(_db(), user), payload
            ))
        if len(parts) == 4 and parts[:2] == ["api", "feedback"] and parts[3] == "replies":
            return self._result(api.reply_feedback(
                _db(), int(parts[2]), user, auth.role_of(user), payload
            ))
        if len(parts) == 4 and parts[:2] == ["api", "feedback"] and parts[3] == "close":
            return self._result(api.close_feedback(
                _db(), int(parts[2]), user, auth.role_of(user)
            ))
        if path == "/api/settings/test":
            if not self._cap_left(user):
                return self._cap_exhausted()
            return self._result(api.probe_llm(_db(), payload, user))
        if len(parts) == 4 and parts[:2] == ["api", "import"] and parts[3] == "confirm":
            self._audit("confirm_import", str(parts[2]))
            return self._result(api.confirm_import(
                _db(), int(parts[2]), payload, role, self._user()
            ))
        if len(parts) == 4 and parts[:2] == ["api", "problems"]:
            problem_id = int(parts[2])
            payload = {**payload, "id": problem_id}
            if parts[3] == "datasets":
                if not self._cap_left(user):
                    return self._cap_exhausted()
                if user_llm.config_for(_db(), user) is None:
                    return self._no_key()
                self._audit("start_dataset", str(problem_id))
                return self._result(api.start_dataset(_db(), payload, user))
            if parts[3] == "submissions":
                if not self._cap_left(user):
                    return self._cap_exhausted()
                if user_llm.config_for(_db(), user) is None:
                    return self._no_key()
                self._audit("start_adversarial", str(problem_id))
                return self._result(api.start_adversarial(_db(), payload, user))
            if parts[3] == "delete":
                self._audit("delete_problem", str(problem_id), str(payload.get("confirm_title") or "")[:120])
                return self._result(api.delete_problem(
                    _db(), problem_id, str(payload.get("confirm_title") or ""), role, user
                ))
            if parts[3] == "restore-statement":
                self._audit("restore_statement", str(problem_id))
                return self._result(api.restore_statement(_db(), problem_id))
            if parts[3] == "cancel":
                return self._result(api.cancel_job(_db(), payload))
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            return self._result(api.cancel_job_by_job(_db(), int(parts[2])))
        return self._fail(404, "NOT_FOUND", "接口不存在")

    #: 连接级事件（读超时、客户端中途断开）：HTTP/1.1 长连接下这是**常态**，
    #: 175 个学生同时挂着页面就是每分钟几百次。这类事件不写日志：
    #: 以前它们会走 log_error → log_message → self.command（超时时还没赋值）→
    #: AttributeError，每次刷三十行堆栈，把真正的错误淹掉，还白占磁盘。
    _QUIET_ERRORS = ("timed out", "Connection reset", "Broken pipe", "Connection aborted")

    def log_message(self, fmt: str, *args: Any) -> None:
        # 超时路径上 self.command/self.path 可能还没赋值，取不到就记 "-"
        command = getattr(self, "command", None) or "-"
        path = getattr(self, "path", None) or "-"
        print(f"[slim2] {_client_ip(self)} {command} {path} - {fmt % args}", flush=True)

    def log_error(self, fmt: str, *args: Any) -> None:
        text = fmt % args
        if any(item in text for item in self._QUIET_ERRORS):
            return
        super().log_error(fmt, *args)


def _int(query: Dict[str, list], name: str, default: int) -> int:
    try:
        return int(str((query.get(name) or [""])[0]))
    except (TypeError, ValueError):
        return default


class _Server(ThreadingHTTPServer):
    """Windows 上 SO_REUSEADDR 允许两个进程绑同一端口，第二个实例会"假装启动成功"，
    请求随机落到其中一个 —— 现象就是"明明启动了却连不上/数据对不上"。
    这里在 Windows 关掉端口复用，让冲突立刻变成一句人话。

    另加并发闸门：HTTP/1.1 每个连接占一个线程，被慢连接或大量请求打满后就再也进不来人。
    闸门满了直接回 503 并断开，比整站卡死好排查。
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(config.MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\nRetry-After: 3\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve() -> None:
    config.apply_env()
    host = config.host()
    port = config.port()
    db = _db()
    auth.purge_expired(db)
    user, _stored, fresh = auth.load_credentials()
    try:
        httpd = _Server((host, port), Handler)
    except OSError as exc:
        print(f"启动失败：{host}:{port} 无法绑定（{exc}）", flush=True)
        print("多半是已经有一个实例在跑：先关掉那个窗口，或设置 AUTOCODE_SLIM2_PORT 换端口。", flush=True)
        raise SystemExit(1)
    # 端口抢到了再动任务：否则第二个实例会把同一个队列重复拉起
    try:
        orphans = jobs.mark_orphans(db)
        if orphans:
            print(f"上次运行留下的 {orphans} 个任务已标记为中断（服务重启时进程被终止）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"对账历史任务失败（不影响启动）：{exc}", flush=True)
    try:
        resumed = jobs.dispatch(db)
        jobs.ensure_scheduler(db)
        if resumed:
            print(f"已从排队区接着启动 {resumed} 个任务", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"任务调度器启动失败（正在跑的任务不受影响，但排队的不会自动开始）：{exc}", flush=True)
    print(f"精简版已启动：http://{host}:{port}/  用户：{user}", flush=True)
    if fresh:
        print(f"首次启动，随机密码已写入 {config.initial_password_path()}", flush=True)
    print(f"数据库：{config.db_path()}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
