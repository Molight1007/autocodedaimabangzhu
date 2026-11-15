"""HTTP 适配层安全测试（B1，审查阻断项）。

真起一个 `ThreadingHTTPServer` 在随机端口上，用 `http.client` 造真实请求——不 mock，
因为要验的正是"请求到达 handler 之前/之时"的行为：

1. `Host: evil.example` → 403（挡 DNS rebinding）
2. `Content-Type: text/plain` → 415（挡 CORS 简单请求 CSRF）
3. `Origin: https://evil.example` → 403
4. body > 2MB → 413
5. `/api/run` 第 3 个并发 → 429
6. 合法请求仍然 200（回归）
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autocode.store.db import Database, reset_default_db
from autocode.web import server as web_server


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "autocode.db"
        self.env = patch.dict(os.environ, {"AUTOCODE_DB": str(self.db_path)}, clear=False)
        self.env.start()
        reset_default_db()
        db = Database(self.db_path)
        db.init_schema()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(title, statement_md, spec_json, status, created_at, updated_at)"
                " VALUES('安全测试题', '题面', ?, 'draft', 'now', 'now')",
                (json.dumps({"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}),),
            )
        # 每个测试用全新的信号量，避免用例之间互相影响
        self._sem_patch = patch.object(web_server, "_HEAVY_SLOTS", threading.Semaphore(2))
        self._sem_patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._sem_patch.stop()
        reset_default_db()
        self.env.stop()
        self._tmp.cleanup()

    # ── 请求工具 ────────────────────────────────────────────────────────────
    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict | None = None,
        host: str | None = None,
    ) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            if host is not None:
                conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host)
                for key, value in (headers or {}).items():
                    conn.putheader(key, value)
                if body:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                if body:
                    conn.send(body)
            else:
                conn.request(method, path, body=body or None, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {"_raw_len": len(raw)}
            return int(response.status), payload
        finally:
            conn.close()

    def post_json(self, path: str, payload: dict, **kwargs) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", {}) or {})
        return self.request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers=headers,
            **kwargs,
        )


class TestHostGuard(ServerCase):
    def test_evil_host_rejected(self) -> None:
        status, payload = self.request("GET", "/api/problems", host="evil.example")
        self.assertEqual(status, 403)
        self.assertIn("host", payload["error"])

    def test_evil_host_with_port_rejected(self) -> None:
        status, _ = self.request("GET", "/api/problems", host="evil.example:8765")
        self.assertEqual(status, 403)

    def test_localhost_with_port_allowed(self) -> None:
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", "[::1]:1"):
            status, _ = self.request("GET", "/api/problems", host=host)
            self.assertEqual(status, 200, host)


class TestContentTypeGuard(ServerCase):
    def test_text_plain_csrf_rejected(self) -> None:
        """审查里的原始攻击：text/plain 简单请求不触发预检，必须 415。"""
        body = json.dumps({"title": "CSRF", "statement": "题面"}).encode("utf-8")
        status, payload = self.request(
            "POST",
            "/api/import",
            body=body,
            headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", payload["error"])
        reset_default_db()
        db = Database(self.db_path)
        self.assertEqual(int(db.query_scalar("SELECT COUNT(*) FROM problems")), 1)
        db.close()

    def test_json_accepted(self) -> None:
        status, payload = self.post_json(
            "/api/import", {"title": "正常题", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"}
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("id", payload)


class TestOriginGuard(ServerCase):
    def test_evil_origin_rejected(self) -> None:
        status, payload = self.post_json(
            "/api/import",
            {"title": "CSRF2", "statement": "题面"},
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])

    def test_same_origin_allowed(self) -> None:
        status, payload = self.post_json(
            "/api/import",
            {"title": "同源题", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"},
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 200, payload)

    def test_same_host_other_port_rejected(self) -> None:
        """一般项2：本机另一个端口的页面（:6666）不能被当成同源。"""
        status, payload = self.post_json(
            "/api/import",
            {"title": "端口不符", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"},
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:6666"},
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])

    def test_https_scheme_origin_rejected(self) -> None:
        """Origin 必须是 http（本服务无 TLS）：https 同端口也不放行。"""
        status, payload = self.post_json(
            "/api/import",
            {"title": "https 源", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"},
            headers={
                "Content-Type": "application/json",
                "Origin": f"https://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", payload["error"])

    def test_other_hostname_same_port_rejected(self) -> None:
        """Host 是 127.0.0.1 而 Origin 写 localhost（或反之）不算同源。"""
        status, _ = self.post_json(
            "/api/import",
            {"title": "host 不符", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"},
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://localhost:{self.port}",
            },
        )
        self.assertEqual(status, 403)

    def test_no_origin_allowed(self) -> None:
        """curl/脚本不带 Origin，放行（否则命令行无法调试）。"""
        status, _ = self.post_json(
            "/api/import", {"title": "无源题", "statement": "### 【样例输入】\n1\n### 【样例输出】\n1\n"}
        )
        self.assertEqual(status, 200)


class TestBodyLimit(ServerCase):
    def test_oversize_body_413(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/import")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(web_server.MAX_BODY_BYTES + 1))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            conn.close()


class TestHeavyConcurrency(ServerCase):
    def test_third_concurrent_run_gets_429(self) -> None:
        """`/api/run` 限并发 2：两个已在执行时，第三个必须 429（不排队）。

        为了让"两个真的已经在 handler 里"可确定，这里把业务函数换成"打点 + 睡 1s"的桩：
        信号量的获取/释放仍走真实 handler 代码，只把耗时部分变成可控。
        """
        import time as _time

        inside = threading.Event()
        state = {"count": 0}
        lock = threading.Lock()

        def fake_run_student_code(_payload):
            with lock:
                state["count"] += 1
                if state["count"] >= 2:
                    inside.set()
            _time.sleep(1.0)
            return {"ok": True, "total": 0, "runs": []}

        statuses: list[int] = []
        with patch.object(web_server.api, "run_student_code", fake_run_student_code):
            def worker() -> None:
                status, _ = self.post_json("/api/run", {"problem_id": 1, "lang": "python", "code": "x"})
                with lock:
                    statuses.append(status)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(inside.wait(timeout=10), "两个请求没有同时进入 handler")
            third_status, third_payload = self.post_json(
                "/api/run", {"problem_id": 1, "lang": "python", "code": "x"}
            )
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(third_status, 429, third_payload)
        self.assertEqual(sorted(statuses), [200, 200])


if __name__ == "__main__":
    unittest.main()
