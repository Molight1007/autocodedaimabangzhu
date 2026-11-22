"""流水线失败：`stage` 决定前端显示"卡在哪一步"，`message` 直接给人看。"""

from __future__ import annotations


class StageFailure(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = str(stage or "unknown")
        self.message = str(message or "生成失败")
