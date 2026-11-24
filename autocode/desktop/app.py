"""桌面版入口：原生窗口 + 进程内 JS 桥，不需要 HTTP 端口。

契约 §1：本文件**只做 JS bridge 映射**，业务一律调用 `autocode/web/api.py`
（与网页版同一份实现），因此两端行为一致。

启动：python -m autocode.desktop
或双击项目根目录的 `autoCode.bat`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from ..web import api as api

STATIC = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"


class Bridge:
    """前端通过 window.pywebview.api.request(path, payload, method) 调进来。"""

    def reveal(self, path: str) -> bool:
        import os

        target = Path(path)
        if not target.exists():
            return False
        os.startfile(str(target.parent if target.is_file() else target))  # noqa: S606
        return True

    def request(self, path: str, payload: Dict[str, Any] | None = None, method: str = "GET") -> Any:
        payload = payload or {}
        try:
            return dispatch(path, payload, method)
        except Exception as exc:  # noqa: BLE001 - 桥接层必须把异常变成可展示的错误
            return {"error": str(exc)[:400]}


def dispatch(path: str, payload: Dict[str, Any], method: str = "GET") -> Any:
    """路径 → 业务函数映射（与 server.py 的路由表一一对应）。"""
    verb = method.upper()
    if path == "/api/overview":
        return api.overview()
    if path == "/api/problems":
        return api.problem_list()
    if path == "/api/quality":
        return api.quality()
    if path == "/api/settings":
        if verb == "POST":
            api.update_fields(payload)
        return api.settings_view()
    if path == "/api/settings/test":
        return api.test_connection()
    if path == "/api/import":
        return api.import_problem(payload)
    if path == "/api/import/pdf":
        return api.import_pdf(payload)
    if path == "/api/import/batch":
        return api.import_batch(payload)
    if path == "/api/generate":
        return api.start_generate(payload)
    if path == "/api/export":
        return api.export_problem(int(payload.get("id") or 0))
    if path == "/api/run":
        return api.run_student_code(payload)
    if path == "/api/diagnose":
        return api.diagnose_student_code(payload)
    if path == "/api/adversarial":
        return api.generate_adversarial(payload)
    if path.startswith("/api/problems/"):
        return _dispatch_problem(path, payload, verb)
    return {"error": f"未知接口：{path}"}


def _dispatch_problem(path: str, payload: Dict[str, Any], verb: str) -> Any:
    parts = [item for item in path.split("/") if item]
    if len(parts) < 3:
        return {"error": f"未知接口：{path}"}
    problem_id = int(parts[2])
    if len(parts) == 3:
        return api.problem_detail(problem_id)
    sub = parts[3]
    if verb == "POST" and sub == "cases":
        return api.merge_cases({**payload, "problem_id": problem_id})
    if verb == "POST" and sub == "delete":
        return api.delete_problem(problem_id)
    if sub == "quality":
        return api.problem_quality(problem_id)
    if sub == "exports":
        return api.list_exports(problem_id)
    if sub == "job":
        return api.job_status(problem_id)
    return {"error": f"未知接口：{path}"}


def main() -> int:
    import webview

    if not STATIC.exists():
        print(f"找不到前端页面：{STATIC}", file=sys.stderr)
        return 1
    # 桌面版没有 HTTP 服务，调度线程要在这里起：否则第一题跑完后，排队的第二题不会自动开始
    api.ensure_job_scheduler()
    webview.create_window(
        "autoCode · 出题工作台",
        # 用 file:// URI 而不是裸路径：裸路径会被 pywebview 的内建 HTTP 服务器接管，
        # query 丢失后前端会误判成浏览器模式，POST /api/* 打到静态服务器 → 405。
        # 注意：file:// 上不能再拼 `?desktop=1`，WebView2 会把它当文件名 → ERR_FILE_NOT_FOUND；
        # 桌面判定已由 `location.protocol === 'file:'` 与 `window.pywebview` 覆盖。
        url=STATIC.as_uri(),
        js_api=Bridge(),
        width=1440,
        height=920,
        min_size=(1080, 700),
        text_select=True,
    )
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
