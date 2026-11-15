"""autoCode 自治内核包。

职责：
- 协议词表与请求校验（contracts/）
- 动作分发与自治循环（controller/）
- 会话状态与产物会话（session/）
- 受限执行器（runtime/，P1 将由 engine/runner 重写替换）
- 对外 Facade（facade.py）与追踪日志（trace.py）

约束：
- 本包不得 import ui/；engine/ 亦不得反向 import 本包（R2）。
- 一产物一会话：产物级状态放 ArtifactSession，不放模块级全局变量。
"""

from __future__ import annotations

__all__: list[str] = []
