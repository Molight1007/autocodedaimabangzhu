"""Isolated Agent 阶段 1-7 对外导出。

当前稳定入口：
- `create_session`: 创建会话状态
- `dispatch_action`: 对单动作请求做校验与分发
- `run_autonomous_session`: 启动阶段 5 自治循环
- `solve_with_agent`: 单入口 facade
"""

from .controller import create_session, dispatch_action, run_autonomous_session
from .facade import solve_with_agent
from .runtime import build_default_run_executor
from .session import AgentSessionState

__all__ = [
    "AgentSessionState",
    "build_default_run_executor",
    "create_session",
    "dispatch_action",
    "run_autonomous_session",
    "solve_with_agent",
]
