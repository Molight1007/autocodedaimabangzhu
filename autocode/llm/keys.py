"""API key 文件定位与读取。

单独成文件是为了让 `client.py` 守住 800 行硬线（R4 门禁）；
这里的路径推导与 client 同目录，`parents[2]` 仍指向仓库根。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def project_key_file() -> Path:
    """`<repo>/File/key.txt`（已 gitignore）。"""
    return Path(__file__).resolve().parents[2] / "File" / "key.txt"


def read_key_file(path: Optional[Path] = None) -> str:
    """读本地 key 文件；不存在/空则返回空串（不抛，交由上层决策）。"""
    target = Path(path) if path is not None else project_key_file()
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
