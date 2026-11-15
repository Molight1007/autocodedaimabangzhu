"""autoCode 纯工具包。

约束：禁 import 任何 autocode 业务包（agent/engine/llm/store/app/ui），
只允许 stdlib 与 autocode 之外的第三方依赖。
"""

from __future__ import annotations

__all__: list[str] = []
