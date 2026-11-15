"""Isolated Agent 阶段 6：上下文裁剪器与摘要器。

核心职责：
- 提供题目卡、事件摘要、动态上下文等兼容工具函数。
- 为旧上下文拼装路径和调试场景保留可复用的裁剪能力。
- 支持将会话事件裁剪为可控窗口（默认最近 15 轮，最大 30，可配置）。

不负责：
- 不分发动作，不执行 run，不修改会话状态。
- 不处理隐藏测试明文数据清洗（调用方不得传入该类数据）。

数据流：
- 兼容调用方传入 `problem + recent_events + attempt_idx` ->
  本模块返回裁剪后的摘要文本。

约束：
- 裁剪器只按顺序保留尾部窗口，不重排事件。
- 当前主自治循环已经切换到 `messages.py` 的固定消息窗口；本模块不再负责主链路消息构造。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from ..contracts.protocol import DEFAULT_CONTEXT_ROUNDS, MAX_CONTEXT_ROUNDS

_DEFAULT_PROBLEM_CARD_MAX_CHARS = 1800
_DYNAMIC_CONTEXT_MAX_CHARS = 2600
_RECENT_EVENTS_MAX_CHARS = 900
_READ_PREVIEW_MAX_CHARS = 1200
_VERSIONS_MAX_CHARS = 500
_FIELD_VALUE_MAX_CHARS = 600
_HEAVY_PROBLEM_KEYS = {"cases", "data_samples", "samples", "generate_data_code", "cross_feedback"}
_ORDERED_PROBLEM_KEYS = (
    "title",
    "description",
    "input_format",
    "output_format",
    "constraints",
    "stdin",
    "stdout",
    "sample_input",
    "sample_output",
)


def trim_recent_events(events: Iterable[Dict[str, Any]], *, max_rounds: int) -> List[Dict[str, Any]]:
    window = min(MAX_CONTEXT_ROUNDS, max(1, int(max_rounds or DEFAULT_CONTEXT_ROUNDS)))
    data = [dict(item or {}) for item in list(events or [])]
    if len(data) <= window:
        return data
    return data[-window:]


def summarize_event(event: Dict[str, Any]) -> str:
    action = str((event or {}).get("action") or "")
    result_type = str((event or {}).get("result_type") or "")
    version = str((event or {}).get("version") or "")
    note = str((event or {}).get("note") or "").strip()

    # 核心信号行：action → result_type  v=N
    line = f"- {action} → {result_type}  v={version}"
    if note:
        line += f"  [{note}]"

    # run 失败时附加简短错误摘要，帮助模型理解失败原因，不附加成功输出
    run_err = str((event or {}).get("run_err") or "").strip()
    if run_err:
        line += f"\n  err={run_err[:200]}"

    # self_test 附加稳定性统计，不附加具体输出
    self_test_total = (event or {}).get("self_test_total")
    self_test_ok = (event or {}).get("self_test_ok")
    self_test_all_stable = (event or {}).get("self_test_all_stable")
    if self_test_total is not None:
        line += f"  cases={self_test_ok}/{self_test_total} stable={self_test_all_stable}"

    # protocol_error 附加 error_code，帮助模型修正格式错误
    protocol_message = str((event or {}).get("protocol_message") or "").strip()
    if protocol_message:
        line += f"  proto_err={protocol_message[:100]}"

    return line


def build_problem_card(problem: Dict[str, Any], *, max_chars: int) -> str:
    """构建固定题目卡。

    题目卡会在每轮 user prompt 的前半段稳定保留，用于：
    - 提供持续可缓存的稳定前缀
    - 避免后续轮次因题面摘要漂移而遗忘约束
    - 将超长原题、完整样例等重内容留给 read(target=problem/all)
    """

    limit = max(512, int(max_chars or _DEFAULT_PROBLEM_CARD_MAX_CHARS))
    problem_obj = dict(problem or {})
    lines = [
        "题目卡（固定前缀，完整原题/原始字段请按需 read target=problem 或 all）:",
    ]

    for key in _ORDERED_PROBLEM_KEYS:
        if key not in problem_obj:
            continue
        lines.append(f"{key}:")
        lines.append(_render_problem_value(problem_obj.get(key)))

    extras = {
        str(k): v
        for k, v in sorted(problem_obj.items(), key=lambda item: str(item[0]))
        if str(k) not in _ORDERED_PROBLEM_KEYS and str(k) not in _HEAVY_PROBLEM_KEYS
    }
    if extras:
        lines.append("other_fields:")
        lines.append(_render_problem_value(extras))

    if "cross_feedback" in problem_obj:
        lines.append("cross_feedback:")
        lines.append(_render_problem_value(problem_obj.get("cross_feedback")))

    card = "\n".join(lines)
    if len(card) <= limit:
        return card

    clipped = _clip_text(card, limit=limit)
    return clipped + "\n[题目卡已裁剪；完整原题请 read target=problem 或 all]"


def build_user_context(
    *,
    attempt_idx: int,
    problem: Dict[str, Any],
    session_id: str,
    version: int,
    code_hash: str,
    events: Iterable[Dict[str, Any]],
    max_rounds: int,
    problem_card_max_chars: int,
    version_snapshots: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """生成用户上下文文本。

    规则：
    - 题目卡固定保留在 user prompt 前半段，作为稳定前缀。
    - 状态、事件、read 预览、版本快照等动态区块按优先级裁剪。
    - 完整题目与原始长字段由 read(target=problem/all) 兜底获取。
    """

    recent = trim_recent_events(events, max_rounds=max_rounds)
    latest_read_preview = _find_latest_read_preview(recent)
    problem_text = build_problem_card(problem, max_chars=problem_card_max_chars)
    dynamic_text = build_dynamic_context(
        attempt_idx=attempt_idx,
        session_id=session_id,
        version=version,
        code_hash=code_hash,
        recent_events=recent,
        latest_read_preview=latest_read_preview,
        version_snapshots=version_snapshots,
    )

    return (
        f"{problem_text}\n\n"
        f"{dynamic_text}\n"
        f"请输出下一步 JSON 动作。"
    )


def _find_latest_read_preview(events: List[Dict[str, Any]]) -> str:
    for item in reversed(list(events or [])):
        if str((item or {}).get("action") or "") != "read":
            continue
        preview = str((item or {}).get("read_file_preview") or "").strip()
        if preview:
            return preview
    return ""


def _render_problem_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    return _clip_text(text, limit=_FIELD_VALUE_MAX_CHARS)


def build_dynamic_context(
    *,
    attempt_idx: int,
    session_id: str,
    version: int,
    code_hash: str,
    recent_events: List[Dict[str, Any]],
    latest_read_preview: str,
    version_snapshots: Optional[List[Dict[str, Any]]],
) -> str:
    budget = _DYNAMIC_CONTEXT_MAX_CHARS
    parts = [
        f"动态上下文（可裁剪）:\nattempt={int(attempt_idx)}\nsession_id={session_id}\nversion={version} code_hash={code_hash}",
    ]
    budget -= len(parts[0])

    recent_text = _fit_recent_events(recent_events, max_chars=min(_RECENT_EVENTS_MAX_CHARS, max(200, budget)))
    if recent_text:
        section = f"recent_events:\n{recent_text}"
        parts.append(section)
        budget -= len(section)

    if latest_read_preview and budget > 120:
        preview = _clip_text(
            latest_read_preview,
            limit=min(_READ_PREVIEW_MAX_CHARS, max(120, budget)),
        )
        section = f"最近一次 read 返回内容（已裁剪）:\n{preview}"
        parts.append(section)
        budget -= len(section)

    snapshots_text = _fit_version_snapshots(
        version_snapshots or [],
        max_chars=min(_VERSIONS_MAX_CHARS, max(120, budget)),
    )
    if snapshots_text:
        parts.append(f"available_versions:\n{snapshots_text}")

    return "\n\n".join(parts)


def _fit_recent_events(events: List[Dict[str, Any]], *, max_chars: int) -> str:
    limit = max(120, int(max_chars or _RECENT_EVENTS_MAX_CHARS))
    lines = [summarize_event(item) for item in list(events or [])]
    if not lines:
        return "- <empty>"

    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        extra = len(line) + (1 if kept else 0)
        if kept and used + extra > limit:
            break
        if not kept and len(line) > limit:
            kept.append(_clip_text(line, limit=limit))
            used = len(kept[0])
            break
        kept.append(line)
        used += extra
    kept.reverse()
    return "\n".join(kept)


def _fit_version_snapshots(snapshots: List[Dict[str, Any]], *, max_chars: int) -> str:
    limit = max(120, int(max_chars or _VERSIONS_MAX_CHARS))
    lines = [f"  v{s['version']}: {s['summary']}" for s in list(snapshots or [])]
    if not lines:
        return ""

    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        extra = len(line) + (1 if kept else 0)
        if kept and used + extra > limit:
            break
        if not kept and len(line) > limit:
            kept.append(_clip_text(line, limit=limit))
            used = len(kept[0])
            break
        kept.append(line)
        used += extra
    kept.reverse()
    return "\n".join(kept)


def _clip_text(text: Any, *, limit: int) -> str:
    raw = str(text or "")
    cap = max(80, int(limit or 0))
    if len(raw) <= cap:
        return raw
    head = int(cap * 0.7)
    tail = cap - head
    return raw[:head] + "\n...\n(truncated)\n...\n" + raw[-tail:]
