"""自治对话消息管理器。

核心职责：
- 维护 OpenAI 兼容的消息列表，固定保留 system / problem / current_code 三条消息。
- 将后续每轮交互转换为“assistant 动作摘要 + user 执行结果摘要”两条动态消息。
- 在不裁剪固定消息的前提下，仅对动态消息执行最近 N 轮窗口裁剪。

不负责：
- 不执行任何动作分发或代码运行。
- 不做会话状态持久化；仅负责当前自治循环内的消息构造。
- 不修改 read 逻辑；read 返回的内容仍按 dispatcher 的结果原样写入动态消息。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..contracts.protocol import (
    ACTION_FINALIZE,
    ACTION_MODIFY,
    ACTION_READ,
    ACTION_RUN,
    ACTION_SELF_TEST,
    CODE_CONTEXT_MAX_CHARS,
    DEFAULT_CONTEXT_ROUNDS,
    PROBLEM_CONTEXT_MAX_CHARS,
    RESULT_FINALIZE_OK,
    RESULT_MODIFY_OK,
    RESULT_PROTOCOL_ERROR,
    RESULT_READ_OK,
    RESULT_SELF_TEST_OK,
    RUN_RESULT_TYPES,
)

_RESULT_SNIPPET_MAX_CHARS = 1600
_INVALID_OUTPUT_PREVIEW_MAX_CHARS = 1200


class AutonomousMessageManager:
    """维护自治修复循环的消息窗口。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        problem: Dict[str, Any],
        code_text: str,
        version: int,
        code_hash: str,
        max_rounds: int = DEFAULT_CONTEXT_ROUNDS,
        problem_max_chars: int = PROBLEM_CONTEXT_MAX_CHARS,
        code_max_chars: int = CODE_CONTEXT_MAX_CHARS,
    ) -> None:
        self._system_prompt = str(system_prompt or "").strip() or "You are a helpful assistant."
        self._problem_max_chars = max(512, int(problem_max_chars or PROBLEM_CONTEXT_MAX_CHARS))
        self._code_max_chars = max(512, int(code_max_chars or CODE_CONTEXT_MAX_CHARS))
        self._max_rounds = max(1, int(max_rounds or DEFAULT_CONTEXT_ROUNDS))
        self._problem_text = self._build_problem_message(problem)
        self._code_text = self._build_code_message(
            code_text=code_text,
            version=version,
            code_hash=code_hash,
        )
        self._dynamic_messages: List[Dict[str, str]] = []

    def build_messages(self) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": self._problem_text},
            {"role": "user", "content": self._code_text},
            *[dict(item) for item in self._dynamic_messages],
        ]

    def update_code(self, *, code_text: str, version: int, code_hash: str) -> None:
        self._code_text = self._build_code_message(
            code_text=code_text,
            version=version,
            code_hash=code_hash,
        )

    def append_action_summary(self, *, attempt: int, action_payload: Dict[str, Any]) -> None:
        self._append_dynamic("assistant", self._render_action_summary(attempt=attempt, action_payload=action_payload))

    def append_result_summary(
        self,
        *,
        attempt: int,
        action_payload: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        self._append_dynamic(
            "user",
            self._render_result_summary(
                attempt=attempt,
                action_payload=action_payload,
                result=result,
            ),
        )

    def append_invalid_output(self, *, attempt: int, raw_output: Any) -> None:
        preview = _clip_text(raw_output, limit=_INVALID_OUTPUT_PREVIEW_MAX_CHARS)
        self._append_dynamic(
            "assistant",
            (
                f"第{attempt}轮模型回复无法解析为合法 JSON 动作。"
                f"\n原始回复片段：\n{preview}"
            ),
        )
        self._append_dynamic(
            "user",
            (
                f"第{attempt}轮结果：协议错误。"
                "\n上一轮输出不是可解析的单个 JSON 对象，请下一轮只返回一个合法动作。"
            ),
        )

    def _append_dynamic(self, role: str, content: str) -> None:
        self._dynamic_messages.append({"role": str(role or "user"), "content": str(content or "").strip()})
        keep_messages = self._max_rounds * 2
        if len(self._dynamic_messages) > keep_messages:
            self._dynamic_messages = self._dynamic_messages[-keep_messages:]

    def _build_problem_message(self, problem: Dict[str, Any]) -> str:
        problem_text = _serialize_object(problem)
        body = _clip_text(problem_text, limit=self._problem_max_chars)
        return (
            f"题目原文（固定上下文，超过 {self._problem_max_chars} 字符会裁剪）：\n"
            f"{body}"
        )

    def _build_code_message(self, *, code_text: str, version: int, code_hash: str) -> str:
        code_body = _clip_text(code_text, limit=self._code_max_chars)
        return (
            "当前最新代码（固定上下文，每次代码版本更新都会替换这条消息）：\n"
            f"version={int(version)}\n"
            f"code_hash={str(code_hash or '').strip()}\n"
            "```python\n"
            f"{code_body}\n"
            "```"
        )

    def _render_action_summary(self, *, attempt: int, action_payload: Dict[str, Any]) -> str:
        action = str((action_payload or {}).get("action") or "").strip()
        if action == ACTION_READ:
            target = str((action_payload or {}).get("target") or "code").strip() or "code"
            return f"第{attempt}轮动作：读取上下文，target={target}。"
        if action == ACTION_RUN:
            if bool((action_payload or {}).get("use_current")):
                return f"第{attempt}轮动作：运行当前版本代码。"
            inline_code = str((action_payload or {}).get("code") or "")
            return (
                f"第{attempt}轮动作：运行显式提交的完整代码。"
                f"\n代码统计：{len(inline_code)} 字符，{len(inline_code.splitlines())} 行。"
            )
        if action == ACTION_MODIFY:
            new_code = str((action_payload or {}).get("code") or "")
            summary = str((action_payload or {}).get("summary") or "").strip()
            text = (
                f"第{attempt}轮动作：提交新的完整代码，准备覆盖当前版本。"
                f"\n代码统计：{len(new_code)} 字符，{len(new_code.splitlines())} 行。"
            )
            if summary:
                text += f"\n改动摘要：{summary}"
            return text
        if action == ACTION_SELF_TEST:
            cases = list((action_payload or {}).get("cases") or [])
            return f"第{attempt}轮动作：执行 self_test，自测样例数={len(cases)}。"
        if action == ACTION_FINALIZE:
            code_hash = str((action_payload or {}).get("code_hash") or "").strip()
            if code_hash:
                return f"第{attempt}轮动作：尝试 finalize，并校验 code_hash={code_hash}。"
            return f"第{attempt}轮动作：尝试 finalize 当前版本代码。"
        if action:
            return f"第{attempt}轮动作：{action}。"
        return f"第{attempt}轮动作：<empty>。"

    def _render_result_summary(
        self,
        *,
        attempt: int,
        action_payload: Dict[str, Any],
        result: Dict[str, Any],
    ) -> str:
        action = str((action_payload or {}).get("action") or "").strip()
        result_type = str((result or {}).get("type") or "").strip()

        if result_type == RESULT_READ_OK:
            target = str((result or {}).get("target") or (action_payload or {}).get("target") or "code").strip() or "code"
            file_block = str((result or {}).get("file") or "").rstrip()
            text = (
                f"第{attempt}轮结果：read 成功，target={target}，"
                f"version={int((result or {}).get('version') or 0)}，"
                f"code_hash={str((result or {}).get('code_hash') or '').strip()}。"
            )
            if file_block:
                text += f"\n返回内容：\n{file_block}"
            return text

        if result_type == RESULT_MODIFY_OK:
            return (
                f"第{attempt}轮结果：modify 成功，"
                f"当前版本更新为 v{int((result or {}).get('version') or 0)}，"
                f"code_hash={str((result or {}).get('code_hash') or '').strip()}。"
            )

        if result_type in RUN_RESULT_TYPES:
            text = (
                f"第{attempt}轮结果：{action or 'run'} 执行完成，"
                f"类型={result_type}，"
                f"version={int((result or {}).get('version') or 0)}，"
                f"code_hash={str((result or {}).get('code_hash') or '').strip()}。"
            )
            detail = str((result or {}).get("result") or (result or {}).get("code") or "").strip()
            if detail:
                text += f"\n输出片段：\n{_clip_text(detail, limit=_RESULT_SNIPPET_MAX_CHARS)}"
            return text

        if result_type == RESULT_SELF_TEST_OK:
            items = list((result or {}).get("results") or [])
            total = len(items)
            stable = bool((result or {}).get("all_stable"))
            ok_count = sum(1 for item in items if str((item or {}).get("status") or "") == "ok")
            timeout_count = sum(1 for item in items if str((item or {}).get("status") or "") == "timeout")
            runtime_error_count = sum(
                1 for item in items if str((item or {}).get("status") or "") == "runtime_error"
            )
            return (
                f"第{attempt}轮结果：self_test 完成，稳定={stable}，"
                f"version={int((result or {}).get('version') or 0)}，"
                f"code_hash={str((result or {}).get('code_hash') or '').strip()}。"
                f"\n统计：ok={ok_count}/{total}，timeout={timeout_count}，runtime_error={runtime_error_count}。"
            )

        if result_type == RESULT_FINALIZE_OK:
            return (
                f"第{attempt}轮结果：finalize 成功，"
                f"version={int((result or {}).get('version') or 0)}，"
                f"code_hash={str((result or {}).get('code_hash') or '').strip()}。"
            )

        if result_type == RESULT_PROTOCOL_ERROR:
            error_code = str((result or {}).get("error_code") or "").strip()
            message = str((result or {}).get("message") or "").strip()
            details = dict((result or {}).get("details") or {})
            text = f"第{attempt}轮结果：协议错误"
            if error_code:
                text += f"，error_code={error_code}"
            text += "。"
            if message:
                text += f"\n错误信息：{message}"
            if details:
                text += f"\n错误细节：{_clip_text(_serialize_object(details), limit=_RESULT_SNIPPET_MAX_CHARS)}"
            return text

        return (
            f"第{attempt}轮结果：{action or 'unknown'} 返回了未归类结果。"
            f"\n{_clip_text(_serialize_object(result), limit=_RESULT_SNIPPET_MAX_CHARS)}"
        )


def _serialize_object(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except Exception:
            return str(value)
    return str(value)


def _clip_text(text: Any, *, limit: int) -> str:
    raw = str(text or "")
    cap = max(256, int(limit or 0))
    if len(raw) <= cap:
        return raw
    head = int(cap * 0.7)
    tail = cap - head
    return raw[:head] + "\n...\n(内容过长，已截断)\n...\n" + raw[-tail:]
