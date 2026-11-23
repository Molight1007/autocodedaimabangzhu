"""D0 题面补全：把"只有一段故事、没有输入输出规格"的题补成可执行题面。

为什么要有这一道：
从 PDF / 网页切出来的题，常常只剩题干。这种题直接生成必然失败——模型不知道
该造什么形状的数据，实测会把 32768 token 写满后被截断（题 26）。
与其拒绝，不如先补形式：**只补输入输出格式、范围、样例，不改题意**。

这条链路的三道保险：
1. 提示词把"只补形式"写死；
2. `_keeps_original` 要求新题面里原样保留原文的主要片段，防止模型重写题意；
3. 补完的结果单独落库（`statement_original` + `statement_repaired`），界面显式标注、
   可一键还原，绝不悄悄改掉用户的题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from . import config
from .llm_stage import StageError, StageRunner
from .stages import REPAIR
from .validators import _as_dict, _as_list, _text

CONFIDENCES = ("high", "medium", "low")
#: 新题面至少要包含原文的这么多片段（开头 / 中段 / 结尾各取 30 字）。
#: 少于这个数就说明模型在重写题意，而不是补格式。
MIN_KEPT_FRAGMENTS = 2


@dataclass
class Repaired:
    """一次补全的结论。`ok=False` 时 `reason` 说明为什么补不了。"""

    ok: bool
    statement: str = ""
    added: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    confidence: str = ""
    reason: str = ""
    calls: int = 0
    tokens: int = 0


def build_user(problem: Any, samples: Sequence[Dict[str, Any]] = ()) -> str:
    """补全的输入：原文 + 已有样例（有就给，避免模型重复造）。"""
    statement = str(problem["statement_md"] if hasattr(problem, "__getitem__") else problem)
    parts = [
        "<题目原文>",
        statement.strip(),
        "</题目原文>",
    ]
    if samples:
        parts += ["", "<已解析出的样例>"]
        for index, item in enumerate(samples[:3], start=1):
            parts.append(f"[{index}] 输入：{str(item.get('input', '')).strip()}")
            parts.append(f"    输出：{str(item.get('output', '')).strip()}")
        parts.append("</已解析出的样例>")
    parts += [
        "",
        "<任务>",
        "补全输入格式、输出格式、数据范围；样例没把握就留空。只补形式，不改题意。",
        "</任务>",
    ]
    return "\n".join(parts)


def check(payload: Any, original: str) -> Repaired:
    """校验模型给的补全结果；任何一条不过就当作"补不了"。"""
    obj = _as_dict(payload)
    if not obj:
        return Repaired(False, reason="补全结果不是 JSON 对象")

    complete = obj.get("complete")
    if isinstance(complete, str):
        complete = complete.strip().lower() in ("true", "1", "yes")
    if not complete:
        return Repaired(False, reason=_text(obj.get("unsupported_reason")) or "模型认为这道题补不了")

    statement = str(obj.get("statement") or "").strip()
    # 补全至少要加一段"输入格式"，所以长度必然明显增加；只多出几个字等于没补
    if len(statement) < max(80, len(original) + 30):
        return Repaired(
            False,
            reason=f"补全后的题面只有 {len(statement)} 字（原文 {len(original)} 字），几乎没补东西",
        )
    if not _keeps_original(original, statement):
        return Repaired(False, reason="补全后的题面没有保留原文主体，疑似改写了题意")

    added = [str(item) for item in _as_list(obj.get("added")) if str(item).strip()]
    if not added:
        return Repaired(False, reason="没有说明补了哪几块（added 为空），无法判断是否只补了格式")

    assumptions = [str(item) for item in _as_list(obj.get("assumptions")) if str(item).strip()]
    confidence = _text(obj.get("confidence")) or "medium"
    if confidence not in CONFIDENCES:
        confidence = "medium"

    return Repaired(
        True,
        statement=statement[: config.MAX_REPAIRED_STATEMENT_CHARS],
        added=added[:6],
        assumptions=assumptions[:6],
        confidence=confidence,
    )


def repair(
    runner: StageRunner,
    problem: Any,
    samples: Sequence[Dict[str, Any]] = (),
) -> Repaired:
    """调一次模型补全题面。不抛异常：失败一律转成 `ok=False`。"""
    try:
        payload = runner.call(REPAIR, build_user(problem, samples), attempt=1)
    except StageError as exc:
        return Repaired(False, reason=f"补全调用失败：{exc}", calls=runner.calls, tokens=runner.tokens)

    result = check(payload, str(problem["statement_md"] if hasattr(problem, "__getitem__") else problem))
    result.calls = runner.calls
    result.tokens = runner.tokens
    return result


def _keeps_original(original: str, fixed: str) -> bool:
    """新题面是否原样保留了原文的主要片段（开头 / 中段 / 结尾）。

    只查"字都在"会被常用字骗过；按连续片段查，改写题意必然露馅。
    """
    src = "".join(str(original).split())
    dst = "".join(str(fixed).split())
    if not src:
        return True
    if len(src) < 40:
        return src in dst
    middle = max(0, len(src) // 2 - 15)
    picks = [src[:30], src[middle:middle + 30], src[-30:]]
    return sum(1 for item in picks if item and item in dst) >= MIN_KEPT_FRAGMENTS


def summary(result: Repaired) -> str:
    """给界面/结果用的一句话。"""
    bits = []
    if result.added:
        bits.append("补了：" + "；".join(result.added[:3]))
    if result.assumptions:
        bits.append("假设：" + "；".join(result.assumptions[:3]))
    return "｜".join(bits)
