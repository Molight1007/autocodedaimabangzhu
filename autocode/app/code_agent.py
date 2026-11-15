"""带"试跑"工具的参考解生成（agent loop）。

与一次性生成的区别：模型可以先提交 (code, stdin) 看真实输出，再决定怎么改，
模拟人类"写一点、跑一下"的过程。最后仍要过一次全部样例的硬门。

协议（模型每轮回一个 JSON）：
- {"action":"run","code":"...","stdin":"..."}   → 返回 stdout/stderr
- {"action":"final","code":"..."}               → 用全部样例判分
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..prompts import loader as prompts
from . import judge
from .prep_models import RefArtifact, StageFailure, STAGE_REFERENCES
from .prep_rules import spec_block, statement_excerpt, user_blocks

MAX_STEPS = 8


def _trial(service: Any, lang: str, code: str, stdin: str) -> str:
    batch = judge.run_solution(
        lang, code, [{"id": "trial", "input": stdin, "expected": ""}], checker_type="exact",
        time_limit_ms=5000,
    )
    run = batch.runs[0]
    if batch.compile_error:
        return "编译错误：\n" + batch.compile_error[:800]
    body = (run.stdout or "")[:1200]
    err = (run.stderr or "")[:600]
    return f"stdout:\n{body}\nstderr:\n{err}"


def solve(
    service: Any,
    problem: Dict[str, Any],
    spec: Dict[str, Any],
    samples: List[Dict[str, str]],
    *,
    lang: str = "python",
    max_steps: int = MAX_STEPS,
) -> RefArtifact:
    """让模型在预算内自由试跑，最终返回通过全部样例的解。"""
    cases = [
        {"id": f"sample{i}", "input": s.get("input", ""), "expected": s.get("output", "")}
        for i, s in enumerate(samples, start=1)
    ]
    base = (
        user_blocks(statement_excerpt(problem), samples)
        + f"\n\n## 目标语言\nlang = {lang}\n\n## 已确认的 spec\n"
        + spec_block(spec)
        + "\n\n请按交互协议行动：先用 run 试跑，确认后再 final。"
    )
    transcript = ""
    last_error = ""
    for step in range(1, max_steps + 1):
        result = service._chat(
            prompts.REF_AGENT, base + transcript, tag=f"agent_{lang}#{step}",
            max_tokens=16384, json_mode=True, temperature=0.2,
        )
        payload = result.json_obj or {}
        action = str(payload.get("action") or "").strip().lower()
        code = str(payload.get("code") or "")
        if action == "run":
            stdin = str(payload.get("stdin") or "")
            transcript += (
                f"\n\n### 第 {step} 步：试跑\n输入：\n{stdin[:600]}\n"
                f"结果：\n{_trial(service, lang, code, stdin)}"
            )
            continue
        if action == "final":
            batch = judge.run_solution(
                lang, code, cases,
                checker_type=spec.get("checker_type", "exact"),
                float_eps=float(spec.get("float_eps") or 1e-6),
                time_limit_ms=5000,
            )
            if batch.all_accepted:
                return RefArtifact(
                    lang=lang, role="reference", code=code, samples_total=len(cases),
                    samples_passed=len(cases), verified=True,
                    verify_record={"agent_steps": step, "sample_max_elapsed_ms": batch.max_elapsed_ms},
                    verification_level="cross_verified",
                )
            last_error = judge.failure_hint(batch, cases=cases)
            transcript += f"\n\n### 第 {step} 步：final 未通过\n{last_error[:1500]}"
            continue
        transcript += f"\n\n### 第 {step} 步：无法识别的动作 {action!r}，请用 run 或 final。"
    raise StageFailure(STAGE_REFERENCES, f"agent({lang}) {max_steps} 步内未交出通过样例的解", detail=last_error[:500])
