"""Isolated Agent 阶段 6：上下文裁剪器与摘要器。

核心职责：
- 将会话事件裁剪为可控窗口（默认最近 15 轮，最大 30，可配置）。
- 将事件与题目对象转换为短摘要文本，降低提示词体积并减少噪声。
- 提供“首轮完整题目、后续摘要题目”的上下文组装能力。

不负责：
- 不分发动作，不执行 run，不修改会话状态。
- 不处理隐藏测试明文数据清洗（调用方不得传入该类数据）。

数据流：
- autonomous_loop 传入 `problem + recent_events + attempt_idx` ->
  本模块返回 user 上下文文本 -> 传给 LLM 生成下一动作。

约束：
- 裁剪器只按顺序保留尾部窗口，不重排事件。
- 摘要器只保留机判相关字段，避免将长日志塞入上下文。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..contracts.protocol import DEFAULT_CONTEXT_ROUNDS, MAX_CONTEXT_ROUNDS


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


def summarize_problem(problem: Dict[str, Any], *, max_chars: int) -> str:
    raw = str(problem or "")
    limit = max(256, int(max_chars or 1500))
    if len(raw) <= limit:
        return raw
    head = int(limit * 0.7)
    tail = limit - head
    return raw[:head] + "\n...\n(problem truncated)\n...\n" + raw[-tail:]


def build_user_context(
    *,
    attempt_idx: int,
    problem: Dict[str, Any],
    session_id: str,
    version: int,
    code_hash: str,
    events: Iterable[Dict[str, Any]],
    max_rounds: int,
    problem_summary_max_chars: int,
    version_snapshots: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """生成用户上下文文本。

    规则：
    - 第 1 轮注入完整题目，确保模型首轮决策不依赖 read。
    - 第 2 轮及之后注入题目摘要；若信息不足可通过 read(target=all) 自取。
    - 复杂数据场景可按需 read(target=data_sample) 获取最短测试数据源结构。
    """

    recent = trim_recent_events(events, max_rounds=max_rounds)
    recent_lines = [summarize_event(item) for item in recent]
    recent_text = "\n".join(recent_lines) if recent_lines else "- <empty>"
    latest_read_preview = _find_latest_read_preview(recent)

    if int(attempt_idx) <= 1:
        problem_text = str(problem or {})
        problem_tip = "题目（完整）"
    else:
        problem_text = summarize_problem(problem, max_chars=problem_summary_max_chars)
        problem_tip = "题目（摘要，信息不足请 read target=all，复杂数据可 read target=data_sample）"

    read_section = ""
    if latest_read_preview:
        read_section = (
            "\n\n"
            "最近一次 read 返回内容（已裁剪）：\n"
            f"{latest_read_preview}\n"
        )

    versions_section = ""
    snapshots = list(version_snapshots or [])
    if snapshots:
        versions_lines = [f"  v{s['version']}: {s['summary']}" for s in snapshots]
        versions_section = "\n\navailable_versions:\n" + "\n".join(versions_lines) + "\n"

    return (
        f"{problem_tip}:\n{problem_text}\n\n"
        f"session_id={session_id}\n"
        f"version={version} code_hash={code_hash}\n"
        f"recent_events:\n{recent_text}\n"
        f"{read_section}"
        f"{versions_section}"
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
