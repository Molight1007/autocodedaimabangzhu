"""autoCode 运行层导出。

说明：guard 已按设计迁出到 `autocode.engine.sandbox.agent_guard`（engine 零 LLM 层），
本包只保留 P1 待重写的受限执行器。
"""

from __future__ import annotations

from .executor import build_default_run_executor

__all__ = ["build_default_run_executor"]
