"""逐行代码解析：给每种**不同解法**各生成一份，供 UI 的"代码解析"页按方法切换。

与 `enrich_service.enrich_problem` 的分工：
- enrich 是全量补齐（按方法解析 + 所有语言/角色各一份代码解析，调用数多，CLI 手动跑）；
- 本模块只做 prepare 阶段的**最小可用集**：每种方法挑一份 Python 解（有 Python 时），
  最多 `MAX_METHODS` 份，缺的如实跳过，不为了凑数去解释 C/C++ 版本。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..prompts import loader as prompts
from .prep_rules import spec_block, statement_excerpt, user_blocks

#: 单题最多生成几份（主解 + 两个备选已经够学生对比；再多是烧钱）
MAX_METHODS = 3
RANK = {"reference": 0, "alt1": 1, "alt2": 2, "alt3": 3, "brute": 9}


def _format(data: Dict[str, Any]) -> str:
    lines = [str(data.get("summary") or "").strip()]
    for index, step in enumerate(data.get("steps") or [], start=1):
        if isinstance(step, dict):
            lines.append(f"{index}. {step.get('part', '')}：{step.get('explain', '')}")
    pitfalls = [str(x).strip() for x in (data.get("pitfalls") or []) if str(x).strip()]
    if pitfalls:
        lines.append("易错点：" + "；".join(pitfalls))
    return "\n".join(line for line in lines if line)


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
        body = (
            user_blocks(statement_excerpt(problem), spec.get("samples") or [])
            + f"\n\n## 方法\n{ {'name': name, 'complexity': getattr(artifact, 'complexity', '') or '未声明'} }"
            + f"\n\n## 目标语言\npython\n\n## 代码\n```python\n{str(getattr(artifact, 'code', ''))[:12000]}\n```"
            + "\n\n## 约束\n" + spec_block(spec)
        )
        try:
            result = service._chat(
                prompts.CODE_NOTES,
                body,
                tag=f"code_notes#{pid}_{getattr(artifact, 'role', 'ref')}",
                max_tokens=3072,
                json_mode=True,
            )
            text = _format(result.json_obj or {})
        except Exception as exc:  # noqa: BLE001 - 单份失败不该拖垮整题
            if exc.__class__.__name__ in ("BudgetExhausted", "StageFailure"):
                raise
            service._note(f"{name} 的代码解析失败：{str(exc)[:100]}")
            continue
        if not text:
            service._note(f"{name} 的代码解析为空，已跳过")
            continue
        service.store.save_line_notes(pid, "python", role, text)
        artifact.line_notes = text
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
