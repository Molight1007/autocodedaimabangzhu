"""autoCode 会话层导出。

- `ArtifactSession`：设计决策 8 的一产物一会话模型（generator / 三语言参考解 / 暴力解）。
- `AgentSessionState`：旧内核单会话状态，仅为 P0 过渡保留（设计 §2 标注"不复用/弃"），
  P1 完成 dispatcher/loop 到 ArtifactSession 的重接线后删除。
"""

from __future__ import annotations

from .artifact_session import (
    ARTIFACT_BRUTE,
    ARTIFACT_GENERATOR,
    ARTIFACT_KINDS,
    ARTIFACT_REF_C,
    ARTIFACT_REF_CPP,
    ARTIFACT_REF_PY,
    ArtifactSession,
    open_problem_sessions,
)
from .state import AgentSessionState, compute_code_hash

__all__ = [
    "ARTIFACT_BRUTE",
    "ARTIFACT_GENERATOR",
    "ARTIFACT_KINDS",
    "ARTIFACT_REF_C",
    "ARTIFACT_REF_CPP",
    "ARTIFACT_REF_PY",
    "ArtifactSession",
    "AgentSessionState",
    "compute_code_hash",
    "open_problem_sessions",
]
