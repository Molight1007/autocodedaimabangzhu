"""本地工作台 HTTP 适配层（契约 §1）：**只有路由、状态码、二进制下载**。

安全边界（B1，审查阻断项）：本服务只监听 127.0.0.1，但浏览器里的**任意网页**都能向
`http://127.0.0.1:8765` 发请求（CSRF / DNS rebinding），所以适配层必须自己立门：

1. **Host 白名单**：只接受 127.0.0.1 / localhost / [::1]（可带端口），否则 403。
   这一条挡 DNS rebinding（攻击者把自己的域名解析到 127.0.0.1 来读全量题库）。
2. **POST 强制 `application/json`**：否则 415。`text/plain`、`application/x-www-form-urlencoded`
   属于 CORS 简单请求、不触发预检，攻击者靠它绕开浏览器保护——直接按类型拒掉。
3. **Origin 同源校验**：请求带 Origin 且 host 不在白名单 → 403（不带 Origin 的 curl/脚本放行）。
4. **请求体上限 2MB**：否则 413（防打满内存）。
5. **重活限并发**：`/api/run`、`/api/diagnose` 会编译并执行代码，`Semaphore(2)`，
   拿不到就 429（不排队，避免请求堆积把服务器拖死）。

业务一律在 `autocode/web/api.py`，桌面版 `desktop/app.py` 调用同一份。
启动：python -m autocode.cli ui --port 8765
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

from . import api

STATIC = Path(__file__).resolve().parent / "static"

# ── 安全策略常量（测试直接引用，改这里就是改策略）─────────────────────────
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY_BYTES = 2 * 1024 * 1024
REQUIRED_POST_CONTENT_TYPE = "application/json"
HEAVY_ROUTES = ("/api/run", "/api/diagnose")
HEAVY_CONCURRENCY = 2

_HEAVY_SLOTS = threading.Semaphore(HEAVY_CONCURRENCY)


def split_host_port(host_header: Optional[str]) -> tuple[str, Optional[int]]:
    """把 Host 头拆成 (host, port)。`[::1]:8765` / `127.0.0.1:8765` / `localhost` 都支持。"""
    raw = str(host_header or "").strip()
    if not raw:
        return "", None
    if raw.startswith("["):  # IPv6 字面量
        end = raw.find("]")
        host = raw[1:end] if end > 0 else raw
        rest = raw[end + 1 :] if end > 0 else ""
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        return host.lower(), port
    if raw.count(":") == 1:  # host:port（裸 IPv6 有多个冒号，落到下面的兜底分支）
        host, _, port_text = raw.partition(":")
        return host.strip().lower(), (int(port_text) if port_text.isdigit() else None)
    return raw.lower(), None


def host_allowed(host_header: Optional[str]) -> bool:
    """Host 头是否指向本机。允许 `127.0.0.1:8765`、`[::1]:8765`、`localhost`（大小写不敏感）。"""
    host, _port = split_host_port(host_header)
    return bool(host) and host in ALLOWED_HOSTS


def origin_allowed(origin: Optional[str], host_header: Optional[str] = None) -> bool:
    """Origin 同源校验：空 Origin 放行（非浏览器客户端），否则要求与 `http://{Host}` 完全一致。

    只比 hostname 会放过 `Origin: http://127.0.0.1:6666`（本机另一个 dev server 上的页面）
    和 `https://127.0.0.1:8765`。这里按"scheme + host + port 全等"判定：
    - 本服务是纯 http，所以只接受 scheme=http；
    - Host 带端口时要求 Origin 端口与之相同；
    - Host 不带端口（默认 80 场景）时退化为 host 白名单比对。
    """
    raw = str(origin or "").strip().rstrip("/")
    if not raw:
        return True
    parsed = urlparse(raw)
    if not parsed.hostname:
        return False
    if host_header is None:  # 兼容只传 origin 的调用
        return parsed.hostname.lower() in ALLOWED_HOSTS
    host, host_port = split_host_port(host_header)
    if host not in ALLOWED_HOSTS:
        return False
    if (parsed.scheme or "").lower() != "http":
        return False
    if host_port is None:
        return parsed.hostname.lower() == host
    origin_port = parsed.port or 80
    return parsed.hostname.lower() == host and origin_port == host_port


def content_type_ok(value: Optional[str]) -> bool:
    """POST 必须是 JSON（允许 `application/json; charset=utf-8`）。"""
    main = str(value or "").split(";")[0].strip().lower()
    return main == REQUIRED_POST_CONTENT_TYPE


class Handler(BaseHTTPRequestHandler):
    server_version = "autoCodeUI/2.1"

    def log_message(self, *_args: Any) -> None:  # 静默
        return

    # ── 响应工具 ────────────────────────────────────────────────────────────
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _reject(self, status: int, message: str) -> None:
        """安全拒绝：不读 body，直接回错并关连接。"""
        self.close_connection = True
        self._json({"error": message}, status)

    # ── 安全门 ──────────────────────────────────────────────────────────────
    def _guard(self) -> Optional[int]:
        """返回 None 表示放行；否则返回要回的 HTTP 状态码（已自行响应）。"""
        if not host_allowed(self.headers.get("Host")):
            self._reject(403, "forbidden host")
            return 403
        if self.command == "POST":
            if not content_type_ok(self.headers.get("Content-Type")):
                self._reject(415, "POST 必须是 application/json")
                return 415
            if not origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
                self._reject(403, "forbidden origin")
                return 403
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._reject(400, "invalid content-length")
                return 400
            if length > MAX_BODY_BYTES:
                self._reject(413, f"请求体超过 {MAX_BODY_BYTES} 字节")
                return 413
        return None

    def _read_payload(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"error": "invalid content-length"}, 400)
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"error": "请求体过大或长度非法"}, 413)
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return None
        return payload if isinstance(payload, dict) else {}

    def _serve_export(self, pid: int) -> None:
        """浏览器下载：业务在 api，本层只负责把 zip 流回去。"""
        result = api.export_problem(pid)
        if result.get("error"):
            return self._json(result, 404)
        data = Path(result["path"]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{quote(result['filename'])}"
        )
        self.end_headers()
        self.wfile.write(data)

    # ── GET ────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        if self._guard() is not None:
            return
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(
                    200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"
                )
            if path == "/api/overview":
                return self._json(api.overview())
            if path == "/api/problems":
                return self._json(api.problem_list())
            if path == "/api/quality":
                return self._json(api.quality())
            if path == "/api/settings":
                return self._json(api.settings_view())
            if path.startswith("/api/export/"):
                return self._serve_export(int(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/problems/"):
                return self._get_problem_sub(path)
            return self._json({"error": "not found"}, 404)
        except KeyError:
            return self._json({"error": "problem not found"}, 404)
        except ValueError:
            return self._json({"error": "invalid id"}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)}, 500)

    def _get_problem_sub(self, path: str) -> None:
        """`/api/problems/{id}` 及其子资源（quality / exports / job）。"""
        parts = [item for item in path.split("/") if item]
        pid = int(parts[2])
        if len(parts) == 3:
            return self._json(api.problem_detail(pid))
        sub = parts[3]
        if sub == "quality":
            return self._json(api.problem_quality(pid))
        if sub == "exports":
            return self._json(api.list_exports(pid))
        if sub == "job":
            return self._json(api.job_status(pid))
        return self._json({"error": "not found"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        if self._guard() is not None:
            return
        path = urlparse(self.path).path
        payload = self._read_payload()
        if payload is None:
            return
        heavy = path in HEAVY_ROUTES
        acquired = False
        if heavy:
            acquired = _HEAVY_SLOTS.acquire(blocking=False)
            if not acquired:
                self.send_response(429)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Retry-After", "1")
                body = json.dumps({"error": "并发已满，稍后重试"}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        try:
            self._dispatch_post(path, payload)
        except ValueError:
            self._json({"error": "invalid id"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)
        finally:
            if acquired:
                _HEAVY_SLOTS.release()

    def _dispatch_post(self, path: str, body: dict) -> None:
        if path == "/api/run":
            return self._json(api.run_student_code(body))
        if path == "/api/diagnose":
            return self._json(api.diagnose_student_code(body))
        if path == "/api/adversarial":
            return self._result(api.generate_adversarial(body))
        if path == "/api/generate":
            return self._result(api.start_generate(body))
        if path == "/api/import":
            return self._result(api.import_problem(body))
        if path == "/api/import/pdf":
            return self._result(api.import_pdf(body))
        if path == "/api/import/batch":
            return self._result(api.import_batch(body))
        if path == "/api/settings":
            api.update_fields(body)
            return self._json(api.settings_view())
        if path == "/api/settings/test":
            return self._json(api.test_connection())
        if path == "/api/export":
            return self._result(api.export_problem(int(body.get("id") or 0)))
        if path.startswith("/api/problems/") and path.endswith("/cases"):
            pid = int([item for item in path.split("/") if item][2])
            return self._result(api.merge_cases({**body, "problem_id": pid}))
        if path.startswith("/api/problems/") and path.endswith("/delete"):
            pid = int([item for item in path.split("/") if item][2])
            return self._result(api.delete_problem(pid))
        return self._json({"error": "not found"}, 404)

    def _result(self, payload: dict) -> None:
        """业务返回 `{"error": ...}` 一律 400，其余 200。"""
        self._json(payload, 400 if payload.get("error") else 200)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import webbrowser

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"题库工作台已启动：{url}  (Ctrl+C 退出)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
