"""Python 关键字和内置函数数据。

从 File/python_keywords.json 加载，供语法高亮器和代码补全器共享使用。
数据在模块级别缓存，避免重复读取文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Set


def _load_keywords_data() -> dict:
    """加载关键字数据文件。"""
    json_path = Path(__file__).parent.parent.parent / "File" / "python_keywords.json"
    return json.loads(json_path.read_text(encoding="utf-8"))


_data = _load_keywords_data()

KEYWORDS: List[str] = _data.get("keywords", [])
BUILTINS: List[str] = _data.get("builtins", [])
CALLABLE_BUILTINS: Set[str] = set(_data.get("callable_builtins", []))
STDLIB_MODULES: List[str] = _data.get("stdlib_modules", [])
