"""D2R：参考解复算门 —— 把"模型说对"换成"代码跑出来对"。

为什么需要这一道：
D2 生成期望输出，D3 再独立算一遍。两票一致并不等于正确 —— 两轮都是 LLM 在做
算术，同一类错误（大数、取模、浮点、边界下标）会一起犯，而且很难被对方发现。
真正的判据只有一条：**同一道题的标程，喂进这条输入，吐出来的就是这个输出**。

做法：
1. 让模型写一份 Python 参考解（`slim_reference` 模板，只输出代码块）；
2. 先过静态安全守卫，再交给评测机（沙箱）编译/运行；
3. **样例锚定**：题面样例是老师给的原文，可信度最高。参考解必须在**全部**样例上
   与题面输出一致，否则这份标程不可信，整道题的门作废（不拿它去改任何数据）；
4. 锚定通过后，逐条跑生成的输入，拿到真实输出；
5. 与 D2 的期望输出比对，给出 `agree / corrected / conflict / unverified`。

裁决规则（不做多数决，只认执行结果 + 独立计算）：
- 执行 == D2                       → `agree`     直接采用
- 执行 != D2 且 执行 == D3          → `corrected` 用执行结果覆盖（两票 vs 一票）
- 执行 != D2 且 执行 != D3          → `conflict`  丢弃该条（三份结果互不相同）
- 执行 TLE/RE/MLE（标程自己跑挂）   → `unverified` 不覆盖，交回原流程判断

注意：本模块只产出结论，不写库。入库与计数在 `dataset.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..engine.runner import (
    LANG_PYTHON,
    VERDICT_AC,
    prepare_program,
    release_program,
    run_prepared,
)
from ..engine.sandbox.agent_guard import check_code_safety
from . import config
from .llm_stage import StageError, StageRunner
from .stages import REFERENCE
from .validators import compare_output

# 裁决状态
AGREE = "agree"
CORRECTED = "corrected"
CONFLICT = "conflict"
UNVERIFIED = "unverified"

_FENCE_RE = re.compile(r"```(?:[Pp]ython3?|py)?\s*\n(.*?)```", re.S)


@dataclass
class RunVerdict:
    """单条用例的复算结论。`output` 是标程真跑出来的完整 stdout。"""

    index: int
    status: str
    output: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "status": self.status,
                "output": self.output, "note": self.note}


@dataclass
class ReferenceResult:
    """整道题的门结论。`engaged=False` 表示门没生效（本次数据未被执行验证）。"""

    engaged: bool
    reason: str = ""
    code: str = ""
    samples_passed: int = 0
    samples_total: int = 0
    verdicts: Dict[int, RunVerdict] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0

    def status_of(self, index: int) -> Optional[RunVerdict]:
        return self.verdicts.get(int(index))

    def counts(self) -> Dict[str, int]:
        tally: Dict[str, int] = {AGREE: 0, CORRECTED: 0, CONFLICT: 0, UNVERIFIED: 0}
        for item in self.verdicts.values():
            tally[item.status] = tally.get(item.status, 0) + 1
        return tally


def extract_code(text: str) -> str:
    """从模型回复里取出代码：优先第一个围栏块，没有围栏就整段当代码。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = _FENCE_RE.search(raw)
    if match:
        return match.group(1).strip("\n")
    # 没围栏：去掉常见的"以下是代码："引导行
    lines = [line for line in raw.splitlines() if not line.strip().startswith(("以下是", "代码如下"))]
    return "\n".join(lines).strip("\n")


def _sample_check(
    program: Any, samples: Sequence[Dict[str, Any]], judge: str, tolerance: str,
    timeout_ms: int,
) -> Tuple[int, List[str]]:
    """在题面样例上跑标程，返回 `(通过数, 失败说明)`。"""
    passed = 0
    problems: List[str] = []
    for order, sample in enumerate(samples, start=1):
        run = run_prepared(program, str(sample.get("input", "")), timeout_ms)
        if run["verdict"] != VERDICT_AC:
            problems.append(f"样例{order} 运行 {run['verdict']}")
            continue
        ok, _detail = compare_output(judge, sample.get("output", ""), run["stdout"], tolerance)
        if ok:
            passed += 1
        else:
            problems.append(f"样例{order} 输出不符")
    return passed, problems


@dataclass
class Prepared:
    """标程已落地并（或）已锚定。`program` 非空时由**调用方**负责 release。

    拆成两段是为了并行：写代码只依赖题面，可以先跑；逐条复算要等 D2 出数据。
    """

    result: ReferenceResult
    program: Any = None


def write_and_anchor(
    runner: StageRunner,
    user_content: str,
    *,
    samples: Sequence[Dict[str, Any]],
    judge_type: str,
    tolerance: str = "",
) -> Prepared:
    """让模型写标程并做样例锚定。不抛异常：失败一律转成 `engaged=False`。"""
    try:
        text = runner.call_text(REFERENCE, user_content)
    except StageError as exc:
        return Prepared(ReferenceResult(
            engaged=False, reason=f"参考解不可用：{exc}",
            calls=runner.calls, tokens=runner.tokens,
        ))

    code = extract_code(text)
    if len(code) < 20:
        return Prepared(ReferenceResult(
            engaged=False, reason="参考解为空或过短",
            calls=runner.calls, tokens=runner.tokens,
        ))
    return anchor(code, samples=samples, judge_type=judge_type, tolerance=tolerance,
                  calls=runner.calls, tokens=runner.tokens)


def anchor(
    code: str,
    *,
    samples: Sequence[Dict[str, Any]],
    judge_type: str,
    tolerance: str = "",
    calls: int = 0,
    tokens: int = 0,
) -> Prepared:
    """安全守卫 + 准备程序 + 在全部题面样例上跑一遍。"""
    violation = check_code_safety(code)
    if violation is not None:
        return Prepared(ReferenceResult(
            engaged=False, code=code,
            reason=f"参考解未通过安全守卫（{violation.rule}：{violation.detail[:120]}）",
            calls=calls, tokens=tokens,
        ))

    try:
        program = prepare_program(LANG_PYTHON, code)
    except Exception as exc:  # noqa: BLE001 - 环境问题要变成人话
        return Prepared(ReferenceResult(
            engaged=False, code=code, reason=f"参考解无法准备：{exc}",
            calls=calls, tokens=tokens,
        ))

    result = ReferenceResult(engaged=False, code=code, samples_total=len(samples),
                             calls=calls, tokens=tokens)
    if not program.ok:
        result.reason = "参考解无法运行：" + (program.error or "prepare_failed")
        return Prepared(result, program)
    if not samples:
        # 没样例就没有锚点：这份标程无从证明自己是对的，门按未生效处理
        result.reason = "题面没有样例，标程无法锚定"
        return Prepared(result, program)

    passed, problems = _sample_check(
        program, samples, judge_type, tolerance, int(config.REFERENCE_TIMEOUT_MS)
    )
    result.samples_passed = passed
    if passed != len(samples):
        result.reason = "参考解未通过题面样例（" + "；".join(problems[:3]) + "），门作废"
        return Prepared(result, program)

    result.engaged = True
    return Prepared(result, program)


def run_items(
    prepared: Prepared,
    items: Sequence[Dict[str, Any]],
    judge_type: str,
    tolerance: str = "",
) -> ReferenceResult:
    """逐条复算。门没生效（样例未全过 / 程序不可用）时原样返回。"""
    result = prepared.result
    program = prepared.program
    if not result.engaged or program is None:
        return result

    timeout_ms = int(config.REFERENCE_TIMEOUT_MS)
    for item in items:
        index = int(item["index"])
        run = run_prepared(program, str(item.get("input", "")), timeout_ms)
        if run["verdict"] != VERDICT_AC:
            result.verdicts[index] = RunVerdict(
                index, UNVERIFIED, "",
                f"标程运行 {run['verdict']}（{run['elapsed_ms']:.0f} ms）",
            )
            continue
        actual = str(run["stdout"])
        ok, detail = compare_output(
            judge_type, item.get("expected_output", ""), actual, tolerance
        )
        result.verdicts[index] = RunVerdict(
            index, AGREE if ok else CORRECTED, actual, "" if ok else (detail or "输出不同")
        )
    return result


def verify_by_run(
    runner: StageRunner,
    user_content: str,
    *,
    samples: Sequence[Dict[str, Any]],
    items: Sequence[Dict[str, Any]],
    judge_type: str,
    tolerance: str = "",
    code: str = "",
) -> ReferenceResult:
    """写标程 → 样例锚定 → 逐条复算（串行版本，保留给自测与事后审计）。

    `code` 非空时跳过 LLM，直接用给定标程复算 —— 用于事后审计：
    拿 `quality_json.run_verification.code` 就能把一批数据重新核一遍。
    """
    if code.strip():
        prepared = anchor(code, samples=samples, judge_type=judge_type,
                          tolerance=tolerance, calls=runner.calls, tokens=runner.tokens)
    else:
        prepared = write_and_anchor(runner, user_content, samples=samples,
                                    judge_type=judge_type, tolerance=tolerance)
    try:
        return run_items(prepared, items, judge_type, tolerance)
    finally:
        release_program(prepared.program)


def decide_cases(
    items: Sequence[Dict[str, Any]],
    verdicts: Dict[int, Dict[str, Any]],
    ref: "ReferenceResult",
    judge_type: str,
    tolerance: str = "",
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, Dict[str, Any], Dict[str, Any]]], Dict[int, Dict[str, Any]]]:
    """三方裁决：执行结果（硬证据）+ D2 口算 + D3 独立重算。

    返回 `(可入库条目, 被驳回条目, 每条的决定)`；决定里的 `output` 是最终期望输出，
    `trust` 是入库信任等级（`cross_verified` = 两方一致，`reference_only` = 仅执行验证）。

    这是**唯一**的裁决点：调用方不要再自行判断一遍，否则两条规则迟早会走岔。
    """
    accepted: List[Dict[str, Any]] = []
    rejected: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    decisions: Dict[int, Dict[str, Any]] = {}

    for index, item in enumerate(items):
        verdict = verdicts[index]
        d3 = str(verdict.get("verdict") or "")
        run = ref.status_of(index)
        run_status = run.status if run is not None else ""
        expected = str(item["expected_output"])

        if d3 == "invalid":
            # 输入本身非法，执行结果再对也没用
            decisions[index] = {"action": "reject", "output": expected,
                                "trust": "unreviewed", "note": "输入非法"}
            rejected.append((index, item, verdict))
            continue

        if run_status == AGREE:
            trust = "cross_verified" if d3 == "pass" else "reference_only"
            decisions[index] = {"action": "keep", "output": expected, "trust": trust,
                                "note": "标程真跑与期望输出一致"}
        elif run_status == CORRECTED:
            d3_backs_run = d3 == "mismatch" and _same(judge_type, run.output,
                                                      verdict.get("recomputed_output"), tolerance)
            if d3_backs_run or d3 == "uncertain":
                # 执行结果与 D3 一致，或 D3 本来就算不准（大数/浮点）——
                # 标程已过题面全部样例，此时执行结果比口算可信
                decisions[index] = {"action": "fix", "output": run.output,
                                    "trust": "reference_only", "note": "按标程执行结果修正"}
            else:
                # D3 认同 D2 的口算、但执行结果不同：两份计算 vs 一份代码，不赌
                decisions[index] = {"action": "reject", "output": expected,
                                    "trust": "unreviewed",
                                    "note": "执行结果与两次独立计算都不同"}
                rejected.append((index, item, verdict))
                continue
        else:
            # 门没覆盖到这条（标程 TLE/RE，或整道题门作废）：退回原逻辑
            if d3 == "pass":
                decisions[index] = {"action": "keep", "output": expected,
                                    "trust": "cross_verified", "note": "未经执行复算"}
            else:
                decisions[index] = {"action": "reject", "output": expected,
                                    "trust": "unreviewed",
                                    "note": run.note if run is not None else "未复算"}
                rejected.append((index, item, verdict))
                continue

        materialized = dict(item)
        materialized["expected_output"] = decisions[index]["output"]
        accepted.append(materialized)

    return accepted, rejected, decisions


def _same(judge_type: str, left: Any, right: Any, tolerance: str) -> bool:
    if right is None:
        return False
    ok, _detail = compare_output(judge_type, left, right, tolerance)
    return ok
