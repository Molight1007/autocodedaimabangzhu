"""Isolated Agent 会话核心状态。

核心职责：
- 保存单会话的权威代码状态：`current_code/version/code_hash`。
- 保存最近 N 轮动作摘要，供“短上下文”提示组装使用。
- 记录 finalize 前置条件：最近一次通过运行是否对应当前版本。
- 记录 self_test 稳定性检查结论，确保提交前至少完成一次有效自测。

不负责：
- 不做动作请求解析。
- 不执行代码运行与沙箱行为。

数据来源与生命周期：
- 状态由 controller 在会话创建时初始化，贯穿整个自治会话生命周期。
- `recent_events` 是短期摘要缓存，仅用于最近上下文，不作为长期审计日志。
- `current_code` 是当前权威代码，允许被 modify 覆盖，不保留历史版本代码。
- `runtime_context` 由外层注入运行参数（stdin/期望输出/校验器），允许动态覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Dict, List, Optional


def compute_code_hash(code_text: str) -> str:
    raw = (code_text or "").encode("utf-8", errors="replace")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass
class AgentSessionState:
    """会话状态容器。

    设计要点：
    - `version/code_hash` 用于解决“读取代码与最终提交代码不一致”的并发风险。
    - `last_passed_version` 用于约束 finalize：必须提交最近一次通过测试的版本。
    """

    session_id: str
    current_code: str
    version: int = 1
    code_hash: str = ""
    status: str = "running"
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    max_recent_events: int = 15
    last_run_result_type: str = ""
    last_passed_version: int = -1
    last_self_test_version: int = -1
    last_self_test_all_stable: bool = False
    runtime_context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code_hash:
            self.code_hash = compute_code_hash(self.current_code)
        self.version = max(1, int(self.version or 1))
        self.max_recent_events = min(30, max(1, int(self.max_recent_events or 15)))

    def replace_code(self, code_text: str) -> None:
        self.current_code = str(code_text or "")
        self.version += 1
        self.code_hash = compute_code_hash(self.current_code)

    def mark_run_result(self, result_type: str) -> None:
        self.last_run_result_type = str(result_type or "")
        if self.last_run_result_type == "right":
            self.last_passed_version = int(self.version)

    def can_finalize(self) -> bool:
        return (
            int(self.last_passed_version) == int(self.version)
            and int(self.last_self_test_version) == int(self.version)
            and bool(self.last_self_test_all_stable)
        )

    def mark_self_test_result(self, *, all_stable: bool) -> None:
        self.last_self_test_version = int(self.version)
        self.last_self_test_all_stable = bool(all_stable)

    def push_event(
        self,
        *,
        action: str,
        result_type: str,
        note: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = {
            "action": str(action or ""),
            "result_type": str(result_type or ""),
            "version": int(self.version),
            "code_hash": str(self.code_hash or ""),
            "note": str(note or ""),
        }
        if isinstance(extra, dict) and extra:
            for k, v in extra.items():
                if k in {"action", "result_type", "version", "code_hash", "note"}:
                    continue
                event[str(k)] = v
        self.recent_events.append(event)
        if len(self.recent_events) > int(self.max_recent_events):
            self.recent_events = self.recent_events[-int(self.max_recent_events):]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "version": int(self.version),
            "code_hash": str(self.code_hash or ""),
            "status": str(self.status or "running"),
            "last_run_result_type": str(self.last_run_result_type or ""),
            "last_self_test_version": int(self.last_self_test_version),
            "last_self_test_all_stable": bool(self.last_self_test_all_stable),
            "recent_events": list(self.recent_events),
        }
