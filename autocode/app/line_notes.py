"""逐行代码解析：给每种**不同解法**各生成一份，供 UI 的「代码解析」页按方法切换。

设计要点（v3 2026-11-19）：
- 模型只输出「行号 n + 该行讲解 explain」；**代码原文由本地从已验证参考解按行号拼上**。
  模型无权提供/改写代码，避免抄错行、漏行或把讲解写成代码复述（历史事故：手算复述）。
- 落库 `line_notes_json` 是结构化 JSON（v3），UI 渲染成「行号 | 代码 | 讲解」三列对照。
- 旧版纯文本讲解被 `is_current_notes()` 判为过期，重跑时会自动替换。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..prompts import loader as prompts
from .prep_rules import spec_block, statement_excerpt, user_blocks

#: 逐行解析要覆盖每个非空行（典型 40~50 行）。实测 4096 必然截断，
#: client 翻倍到 16384 重试才成功，那一次用量 7764 tokens —— 8192 只剩 428 余量，
#: 换更长代码照样截断。max_tokens 只是上限（按实际生成量计费），给足不多花钱，
#: 却能消掉「丢弃整次输出 + 翻倍重试」，每份省一次调用和约 17s。
#: 上限由 client 的 MAX_TOKENS_CAP 兜底。
NOTES_MAX_TOKENS = 16384
MAX_METHODS = 3
NOTES_VERSION = 3
#: 模型至少要覆盖这么多比例的非空代码行，否则判为不合格（防止只讲 3 行糊弄）
MIN_COVERAGE = 0.6
RANK = {"reference": 0, "alt1": 1, "alt2": 2, "alt3": 3, "brute": 9}


def number_code(code: str) -> str:
    """给代码每行加 `L<n>:` 前缀，作为发给模型的唯一行号依据。"""
    return "\n".join(
        f"L{i}: {line}" for i, line in enumerate(str(code or "").splitlines(), 1)
    )


def format_line_notes(data: Any, code: str) -> Optional[Dict[str, Any]]:
    """把模型输出与本地真实代码合成逐行对照结构；不合格返回 None。

    代码行永远取自 ``code.splitlines()``；``data`` 里的任何 code 字段一律忽略。
    """
    if not isinstance(data, dict):
        return None
    src = str(code or "").splitlines()
    if not src:
        return None
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        return None
    explains: Dict[int, str] = {}
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(src):
            continue
        text = str(item.get("explain") or "").strip()
        if text and n not in explains:
            explains[n] = text[:400]
    non_empty = [i for i, line in enumerate(src, 1) if line.strip()]
    covered = sum(1 for i in non_empty if i in explains)
    if not non_empty or covered < len(non_empty) * MIN_COVERAGE:
        return None
    lines = [
        {"n": i, "code": src[i - 1], "explain": explains.get(i, "")}
        for i in range(1, len(src) + 1)
    ]
    pitfalls = [
        str(x).strip()[:200] for x in (data.get("pitfalls") or []) if str(x).strip()
    ][:6]
    return {
        "v": NOTES_VERSION,
        "summary": str(data.get("summary") or "").strip()[:600],
        "lines": lines,
        "pitfalls": pitfalls,
        "coverage": f"{covered}/{len(non_empty)}",
    }


def is_current_notes(raw: Any) -> bool:
    """库里的 line_notes_json 是否已是 v3 逐行对照结构（旧纯文本视为过期）。"""
    if isinstance(raw, dict):
        data: Any = raw
    else:
        try:
            data = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return False
    return (
        isinstance(data, dict)
        and data.get("v") == NOTES_VERSION
        and bool(data.get("lines"))
    )


def generate(service: Any, problem: Dict[str, Any], spec: Dict[str, Any], refs: Dict[str, Any]) -> int:
    """返回实际生成份数；任何单份失败只记 note，不影响其它方法。"""
    pid = int(problem["id"])
    ordered = sorted(
        refs.values(),
        key=lambda a: (RANK.get(str(getattr(a, "role", "")), 5), str(getattr(a, "approach", ""))),
    )
    seen: set[str] = set()
    done = 0
    for artifact in ordered:
        if done >= MAX_METHODS:
            break
        if str(getattr(artifact, "lang", "")) != "python":
            continue
        if not getattr(artifact, "verified", 0):
            continue
        name = str(getattr(artifact, "approach", "") or "未标注方法")
        if name in seen:
            continue
        seen.add(name)
        role = str(getattr(artifact, "role", ""))
        if service.store.has_line_notes(pid, "python", role):
            continue
        code = str(getattr(artifact, "code", ""))[:12000]
        body = (
            user_blocks(statement_excerpt(problem), spec.get("samples") or [])
            + "\n\n## 方法\n"
            + json.dumps(
                {"name": name, "complexity": getattr(artifact, "complexity", "") or "未声明"},
                ensure_ascii=False,
            )
            + "\n\n## 目标语言\npython\n\n## 带行号的完整代码（L<n> 为真实行号）\n"
            + number_code(code)
            + "\n\n## 约束\n" + spec_block(spec)
        )
        try:
            result = service._chat(
                prompts.CODE_NOTES,
                body,
                tag=f"code_notes#{pid}_{role}",
                max_tokens=NOTES_MAX_TOKENS,
                json_mode=True,
            )
            payload = format_line_notes(result.json_obj or {}, code)
        except Exception as exc:  # noqa: BLE001 - 单份失败不该拖垮整题
            if exc.__class__.__name__ in ("BudgetExhausted", "StageFailure"):
                raise
            service._note(f"{name} 的逐行代码解析失败：{str(exc)[:100]}")
            continue
        if not payload:
            service._note(f"{name} 的逐行代码解析行号覆盖不足，已跳过")
            continue
        service.store.save_line_notes(pid, "python", role, payload)
        artifact.line_notes = json.dumps(payload, ensure_ascii=False)
        done += 1
    if done:
        service._note(f"已为 {done} 种解法生成逐行代码解析")
    return done


def method_names(refs: Dict[str, Any]) -> List[str]:
    """UI/测试用的方法清单（与生成顺序一致）。"""
    return [
        str(getattr(a, "approach", "") or "未标注方法")
        for a in sorted(refs.values(), key=lambda a: RANK.get(str(getattr(a, "role", "")), 5))
    ]
