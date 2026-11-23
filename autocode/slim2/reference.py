"""D1R：代码答案 —— 先写程序，再用它跑出每一条数据的期望输出。

为什么答案要由代码算：
两轮 LLM 口算一致，并不等于正确 —— 同一类错误（大数、取模、浮点、边界下标）
会一起犯，而且很难被对方发现。真正的判据只有一条：
**同一道题的代码答案，喂进这条输入，吐出来的就是这个输出**。

做法：
1. 让模型写一份 Python 参考解（`slim_reference` 模板，只输出代码块）；
2. 先过静态安全守卫，再交给评测机（沙箱）编译/运行；
3. **样例锚定（可选）**：题面有样例时，代码必须在全部样例上与题面输出一致；
   没有样例时照常执行，只把 `anchored` 标成 False —— 样例是加分项，不是门槛；
4. 逐条跑生成的输入，stdout 直接作为期望输出；跑不动的那条丢弃；
5. D3 独立复核再算一遍：
   - 复核一致            → `cross_verified`
   - 复核算不准          → `reference_only`（执行结果为准）
   - 复核给出不同结果：
       * 代码已过全部样例 → 仍按执行结果（`reference_only`）
       * 没有样例可锚定   → 无法证明谁对，丢弃该条

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
from .validators import MAX_OUTPUT_CHARS, compare_output

# 执行状态：`execute_all` 用前两个，`run_items`（事后审计）用后四个
EXECUTED = "executed"
FAILED = "failed"
AGREE = "agree"
CORRECTED = "corrected"
CONFLICT = "conflict"
UNVERIFIED = "unverified"

_FENCE_RE = re.compile(r"```(?:[Pp]ython3?|py)?\s*\n(.*?)```", re.S)


@dataclass
class RunVerdict:
    """单条用例的复算结论。`output` 是代码答案真跑出来的完整 stdout。"""

    index: int
    status: str
    output: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "status": self.status,
                "output": self.output, "note": self.note}


@dataclass
class ReferenceResult:
    """整道题的门结论。

    `engaged=False` 表示代码答案不可用（编译失败 / 样例不符 / 安全守卫拦下）。
    `anchored` 只表示"题面样例全部通过"；没有样例时它必然是 False，
    但代码仍然可以执行、仍然产出期望输出。
    """

    engaged: bool
    anchored: bool = False
    reason: str = ""
    code: str = ""
    samples_passed: int = 0
    samples_total: int = 0
    #: 题面解析不出样例时，D2 会按契约重写一组。代码答案跑通它们也要记下来：
    #: 这些不是题面原文，不能算 `anchored`，但"完全没验过"也是错的。
    contract_passed: int = 0
    contract_total: int = 0
    verdicts: Dict[int, RunVerdict] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    #: 复核给出不同结果、但代码已过样例而保留的条数（见 `decide_cases`）
    conflicts_kept: int = 0

    def status_of(self, index: int) -> Optional[RunVerdict]:
        return self.verdicts.get(int(index))

    def counts(self) -> Dict[str, int]:
        tally: Dict[str, int] = {
            EXECUTED: 0, FAILED: 0, AGREE: 0, CORRECTED: 0, CONFLICT: 0, UNVERIFIED: 0,
        }
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
    """在题面样例上跑代码答案，返回 `(通过数, 失败说明)`。"""
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
    """代码答案已落地并（或）已锚定。`program` 非空时由**调用方**负责 release。

    写代码只依赖题面；执行要等 D2 出输入，所以拆成"准备"与"跑数据"两段。
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
    """让模型写代码答案并做样例锚定。不抛异常：失败一律转成 `engaged=False`。"""
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
        # 没有样例只是没有"锚"，不是没有代码：照常执行，由 D3 独立复核兜底。
        # 这条是 2026-11-22 改的 —— 旧实现到这里直接判门作废，代码写了却一次没跑。
        result.engaged = True
        result.reason = "题面没有样例，代码答案未锚定（照常执行）"
        return Prepared(result, program)

    passed, problems = _sample_check(
        program, samples, judge_type, tolerance, int(config.REFERENCE_TIMEOUT_MS)
    )
    result.samples_passed = passed
    if passed != len(samples):
        result.reason = "代码答案未通过题面样例（" + "；".join(problems[:3]) + "）"
        return Prepared(result, program)

    result.engaged = True
    result.anchored = True
    return Prepared(result, program)


def check_samples(
    prepared: Prepared,
    samples: Sequence[Dict[str, Any]],
    judge_type: str,
    tolerance: str = "",
) -> List[str]:
    """契约改写样例后，再确认代码读得动它们。

    契约是 D2 按代码的读入方式描述的；两者一旦走岔，样例就是最早暴露的地方。
    返回失败说明，空列表表示全部通过（或无样例可查）。
    """
    result, program = prepared.result, prepared.program
    if not samples:
        return []
    if not result.engaged or program is None:
        return ["代码答案不可用，无法校验样例"]
    passed, problems = _sample_check(
        program, samples, judge_type, tolerance, int(config.REFERENCE_TIMEOUT_MS)
    )
    if passed == len(samples):
        return []
    return [f"代码答案读不了按契约改写的样例（{passed}/{len(samples)} 通过）：" + "；".join(problems[:3])]


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
                f"代码答案运行 {run['verdict']}（{run['elapsed_ms']:.0f} ms）",
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


def execute_all(
    prepared: Prepared,
    items: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """逐条执行代码答案，把 stdout 作为 `expected_output`。

    这是新版流水线的答案来源：LLM 只负责"设计合法、分层的输入"，
    算术交给同一份代码，避免大数/边界口算错误，也解开了"压力层只能出
    闭式可算的小数据"这个自我限制。

    返回 `(可用条目, 丢弃说明)`：单条 TLE/RE 只丢这一条，不影响整批。
    """
    result, program = prepared.result, prepared.program
    if not result.engaged or program is None:
        return [], ["代码答案不可用，无法执行"]
    timeout_ms = int(config.REFERENCE_TIMEOUT_MS)
    kept: List[Dict[str, Any]] = []
    notes: List[str] = []
    # 按下标（不是 item["index"]）编号：丢弃几条后仍与复核批次一一对应
    for index, item in enumerate(items):
        run = run_prepared(program, str(item.get("input", "")), timeout_ms)
        if run["verdict"] != VERDICT_AC:
            notes.append(
                f"第 {index} 条代码运行 {run['verdict']}（{run['elapsed_ms']:.0f} ms），已丢弃"
            )
            result.verdicts[index] = RunVerdict(
                index, FAILED, "", f"{run['verdict']}（{run['elapsed_ms']:.0f} ms）"
            )
            continue
        actual = str(run["stdout"])
        if len(actual) > MAX_OUTPUT_CHARS:
            notes.append(
                f"第 {index} 条输出 {len(actual)} 字符，超过上限 {MAX_OUTPUT_CHARS}，已丢弃"
            )
            result.verdicts[index] = RunVerdict(index, FAILED, "", "输出过长")
            continue
        result.verdicts[index] = RunVerdict(index, EXECUTED, actual)
        row = dict(item)
        row["expected_output"] = actual
        kept.append(row)
    return kept, notes


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
    """写代码答案 → 样例锚定 → 逐条复算（串行版本，保留给自测与事后审计）。

    `code` 非空时跳过 LLM，直接用给定代码答案复算 —— 用于事后审计：
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
    """两方裁决：代码执行结果（`items` 里的期望输出）+ D3 独立复核。

    信任等级的来源必须说清楚：
    - `cross_verified`：复核自己算出来的结果与执行一致，两条独立证据；
    - `reference_only`：只有执行这一条证据（复核算不准，或代码已过全部样例
      但复核算出了别的数）；
    - 复核明确算出不同结果、又没有样例可锚定 → 无法证明谁对，丢弃该条。
      "宁可少一条，不可错一条"：错误答案比缺数据更危险。

    这是**唯一**的裁决点：调用方不要再自行判断一遍，否则两条规则迟早会走岔。
    """
    accepted: List[Dict[str, Any]] = []
    rejected: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    decisions: Dict[int, Dict[str, Any]] = {}

    for index, item in enumerate(items):
        verdict = verdicts[index]
        d3 = str(verdict.get("verdict") or "")
        expected = str(item["expected_output"])

        if d3 == "invalid":
            decisions[index] = {"action": "reject", "output": expected,
                                "trust": "unreviewed", "note": "输入非法"}
            rejected.append((index, item, verdict))
            continue

        if d3 == "pass":
            decisions[index] = {"action": "keep", "output": expected,
                                "trust": "cross_verified",
                                "note": "独立复核与代码执行结果一致"}
        elif d3 == "mismatch":
            if ref.anchored:
                decisions[index] = {
                    "action": "keep", "output": expected, "trust": "reference_only",
                    "note": "代码答案已过全部题面样例，按执行结果",
                }
                ref.conflicts_kept += 1
            else:
                decisions[index] = {
                    "action": "reject", "output": expected, "trust": "unreviewed",
                    "note": "代码答案与独立复核不一致，且没有样例可锚定",
                }
                rejected.append((index, item, verdict))
                continue
        elif d3 == "uncertain":
            decisions[index] = {"action": "keep", "output": expected,
                                "trust": "reference_only",
                                "note": "复核算不准，按代码执行结果"}
        else:
            decisions[index] = {"action": "reject", "output": expected,
                                "trust": "unreviewed", "note": "复核没有给出有效结论"}
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
