"""路由一致性：`web/server.py` 与 `desktop/app.py` 的静态路由必须一一对应。

历史事故（2026-11-17）：新增 `/api/import/pdf` 只加了 HTTP 路由，桌面桥仍是旧表，
用户在桌面端点「读取并切题」得到「未知接口」，而浏览器端一切正常。
两套表各写一遍本身是技术债，这个测试保证它们至少不会悄悄漂移。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_AUTOCODE = Path(__file__).resolve().parents[1]
_ROUTE_RE = re.compile(r'path == "([^"]+)"')


def _routes(relative: str) -> set[str]:
    return set(_ROUTE_RE.findall((_AUTOCODE / relative).read_text(encoding="utf-8")))


class TestRouteParity(unittest.TestCase):
    def test_static_routes_match(self) -> None:
        server = _routes("web/server.py")
        desktop = _routes("desktop/app.py")
        self.assertEqual(
            server,
            desktop,
            f"仅 server 有：{sorted(server - desktop)}；仅 desktop 有：{sorted(desktop - server)}",
        )


if __name__ == "__main__":
    unittest.main()
