"""autoCode LLM 客户端包。

P0 只迁入 analyzer（含 LLM 提示词，按设计 §2 不得留在 engine/）。
`http.py`（重试 + 退避 + JSON mode + usage）与 `config.py` 在 P4 前补齐。
"""

from __future__ import annotations

from .analyzer import analyze_problem

__all__ = ["analyze_problem"]
