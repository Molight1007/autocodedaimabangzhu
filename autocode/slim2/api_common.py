"""api 各子模块共用的小工具：时间戳与分页归一。

单独放一文件是为了拆开 api.py 时不再互相 import：`api_catalog` 与 `api`
都从这里取，谁也不用反向依赖谁。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(str(raw)) if raw else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_limit(value: Any, default: int = 50, cap: int = 200) -> int:
    """归一化分页条数。

    SQLite 里 `LIMIT -1` 表示"不限"，把负数直接透传会把整表读进内存；
    而 `max(1, ...)` 又会把 -3 变成"只返回 1 条"。两种都不对，非正数一律按默认值。
    """
    number = _int(value, default)
    if number <= 0:
        return default
    return min(cap, number)
