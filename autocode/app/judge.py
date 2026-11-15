"""判题与代码质量辅助（app 层，可依赖 engine，不依赖 llm 之外的编排）。

三个职责：
1. `extract_code`：从 LLM 回复里抽出代码块（容忍 ```python / ```cpp / 无围栏）。
2. `run_solution`：把一段代码在一批用例上跑一遍，按 checker 判定 ac/wa/tle/re/ce。
3. `hint_quality_issue`：L1~L3 代码块/可执行代码特征检查（命中打 quality_flag，不阻断）。

计时口径（写进报告）：
- 判题用题面给的 `time_limit_ms`（python 自动 ×4，设计 §6）；
- **造 oracle 的期望输出**用更宽松的 `oracle_timeout_ms`，因为 Python 参考解在最大数据上
  常超题面时限，而这里要的是"算得出来"而非"算得够快"（学生的时限判定在 study 阶段另算）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..engine.runner import (
    LANG_CPP,
    LANG_PYTHON,
    VERDICT_AC,
    VERDICT_CE,
    RunnerEnvError,
    prepare_program,
    release_program,
    run_case,
    run_prepared,
)
from ..engine.verify.comparator import CompareResult, compare

LANG_ALIASES = {
    "python": LANG_PYTHON,
    "py": LANG_PYTHON,
    "python3": LANG_PYTHON,
    "cpp": LANG_CPP,
    "c++": LANG_CPP,
    "cc": LANG_CPP,
    "c": "c",
}
CODE_FENCE_RE = re.compile(r"```[ \t]*(?P<lang>[A-Za-z+#]*)[ \t]*\n(?P<body>.*?)```", re.DOTALL)
PYTHON_SCALE_FACTOR = 4

# L1~L3 里出现的可执行代码特征（命中即打标，不阻断入库）
_CODE_SIGNATURES: Tuple[Tuple[str, str], ...] = (
    ("```", "fenced_code_block"),
    ("#include", "cpp_include"),
    ("def ", "python_def"),
    ("return ", "return_stmt"),
    ("std::", "cpp_std"),
    ("print(", "print_call"),
    ("for (", "c_style_for"),
    ("for(", "c_style_for"),
    ("while (", "c_style_while"),
    ("scanf(", "scanf_call"),
    ("printf(", "printf_call"),
    ("vector<", "cpp_vector"),
    ("->", "member_arrow"),
    ("== null", "code_compare"),
)


@dataclass
class CaseRun:
    """一条用例的执行与判定结果。"""

    case_id: str
    verdict: str
    elapsed_ms: float
    stdout: str = ""
    stderr: str = ""
    compare: Optional[CompareResult] = None

    @property
    def accepted(self) -> bool:
        return self.verdict == VERDICT_AC

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "verdict": self.verdict,
            "elapsed_ms": self.elapsed_ms,
            "accepted": self.accepted,
            "reason": (self.compare.reason if self.compare else ""),
            "detail": (self.compare.detail if self.compare else {}),
            "stderr_head": self.stderr[:400],
            "stdout_head": self.stdout[:400],
        }


@dataclass
class BatchResult:
    """一批用例的整体结果（供阶段门判断与报告）。"""

    lang: str
    runs: List[CaseRun] = field(default_factory=list)
    compile_error: str = ""
    max_elapsed_ms: float = 0.0
    total_elapsed_ms: float = 0.0

    @property
    def all_accepted(self) -> bool:
        return bool(self.runs) and all(run.accepted for run in self.runs)

    @property
    def failures(self) -> List[CaseRun]:
        return [run for run in self.runs if not run.accepted]

    def first_failure(self) -> Optional[CaseRun]:
        failures = self.failures
        return failures[0] if failures else None

    def verdict_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for run in self.runs:
            counts[run.verdict] = counts.get(run.verdict, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lang": self.lang,
            "cases": len(self.runs),
            "accepted": sum(1 for run in self.runs if run.accepted),
            "verdicts": self.verdict_counts(),
            "max_elapsed_ms": self.max_elapsed_ms,
            "total_elapsed_ms": self.total_elapsed_ms,
            "compile_error": self.compile_error[:400],
        }


def normalize_lang(lang: str) -> str:
    key = str(lang or "").strip().lower()
    if key not in LANG_ALIASES:
        raise ValueError(f"未知语言 {lang!r}")
    return LANG_ALIASES[key]


def extract_code(text: str, lang: str = LANG_PYTHON) -> str:
    """从模型回复里抽代码：优先匹配指定语言的围栏，其次任意围栏，最后当纯代码处理。"""
    raw = str(text or "")
    wanted = normalize_lang(lang)
    blocks = [
        (match.group("lang").strip().lower(), match.group("body"))
        for match in CODE_FENCE_RE.finditer(raw)
    ]
    for fence_lang, body in blocks:
        if fence_lang and LANG_ALIASES.get(fence_lang) == wanted:
            return body.strip("\n")
    if blocks:
        return blocks[0][1].strip("\n")
    return raw.strip()


def hint_quality_issue(content: str) -> Optional[str]:
    """L1~L3 代码块检测；返回命中原因（None 表示干净）。"""
    text = str(content or "")
    hits = [label for token, label in _CODE_SIGNATURES if token in text]
    return ",".join(dict.fromkeys(hits)) if hits else None


def time_limit_for(time_limits_ms: Dict[str, Any], lang: str) -> int:
    """取该语言的时限；python 按 ×4 放宽（设计 §6 时间限制分语言）。"""
    base = int(time_limits_ms.get("c") or time_limits_ms.get("cpp") or 1000)
    if normalize_lang(lang) == LANG_PYTHON:
        return int(time_limits_ms.get("python") or base * PYTHON_SCALE_FACTOR)
    if normalize_lang(lang) == LANG_CPP:
        return int(time_limits_ms.get("cpp") or base)
    return int(time_limits_ms.get("c") or base)


def run_solution(
    lang: str,
    code: str,
    cases: Sequence[Dict[str, Any]],
    *,
    checker_type: str = "exact",
    float_eps: float = 1e-6,
    time_limit_ms: int = 2000,
    compare_expected: bool = True,
) -> BatchResult:
    """把一份代码在用例上跑一遍。

    `cases` 每项需含 `id` 与 `input`（`expected` 可选，`compare_expected=False` 时忽略）。
    """
    language = normalize_lang(lang)
    batch = BatchResult(lang=language)
    # S7：整批只编译一次（旧实现每用例重编译，cpp 20 条要 12.3s）
    try:
        program = prepare_program(language, code)
    except RunnerEnvError as exc:
        batch.compile_error = f"{exc}"
        return batch
    try:
        if not program.ok:
            batch.compile_error = (program.compile_output or program.error)[:2000]
            return batch
        for case in cases:
            case_id = str(case.get("id") or len(batch.runs))
            expected = case.get("expected")
            result = run_prepared(
                program,
                str(case.get("input") or ""),
                int(time_limit_ms),
            )
            elapsed = float(result.get("elapsed_ms") or 0.0)
            batch.total_elapsed_ms += elapsed
            batch.max_elapsed_ms = max(batch.max_elapsed_ms, elapsed)
            verdict = str(result.get("verdict") or "re")
            compared: Optional[CompareResult] = None
            if verdict == VERDICT_CE:
                batch.compile_error = str(
                    result.get("compile_output") or result.get("stderr") or ""
                )[:2000]
            if verdict == VERDICT_AC and compare_expected and expected is not None:
                compared = compare(checker_type, str(expected), str(result.get("stdout") or ""), float_eps=float_eps)
                if not compared.ok:
                    verdict = "wa"
            batch.runs.append(
                CaseRun(
                    case_id=case_id,
                    verdict=verdict,
                    elapsed_ms=elapsed,
                    stdout=str(result.get("stdout") or ""),
                    stderr=str(result.get("stderr") or ""),
                    compare=compared,
                )
            )
            if verdict == VERDICT_CE:
                break
        return batch
    finally:
        release_program(program)


def batch_outputs(batch: BatchResult) -> Dict[str, str]:
    """把批量结果压成 `{case_id: stdout}`，只保留**干净跑完**的用例。

    TLE/RE 的输出是半截的，拿它当期望值会把错数据永久写进题库（设计 §5 ④）。
    """
    return {run.case_id: run.stdout for run in batch.runs if run.verdict == VERDICT_AC}


def failure_hint(
    batch: BatchResult,
    *,
    cases: Optional[Sequence[Dict[str, Any]]] = None,
    limit: int = 3,
) -> str:
    """把失败信息压成给 LLM 的修复提示：**带失败用例输入 + 期望/实际 + 完整 stderr**。

    只给"第几行不一致"模型无法复现；必须把该用例的输入原文一起喂回去，
    否则它只能猜题意，重试多少次都改不对。
    """
    by_id = {str(case.get("id")): case for case in (cases or [])}
    lines: List[str] = []
    if batch.compile_error:
        lines.append("编译错误原文：\n" + batch.compile_error[:1500])
    for run in batch.failures[:limit]:
        case = by_id.get(str(run.case_id)) or {}
        text = str(case.get("input") or "")
        if text:
            lines.append(f"用例 {run.case_id} 的输入（前 500 字）：\n{text[:500]}")
        if run.compare is not None and not run.compare.ok:
            detail = run.compare.detail
            expected = detail.get("expected_context") if isinstance(detail, dict) else None
            actual = detail.get("actual_context") if isinstance(detail, dict) else None
            lines.append(
                f"用例 {run.case_id} 判 {run.verdict}：{run.compare.reason}；"
                f"期望={expected}；实际={actual}；定位={detail}"
            )
        else:
            lines.append(
                f"用例 {run.case_id} 判 {run.verdict}：stderr={run.stderr[:1200]!r}"
            )
    return "\n".join(lines)


def iter_case_payload(cases: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """只保留 id/input/expected 三个字段（喂给执行层，避免多余字段漏进去）。"""
    payload: List[Dict[str, str]] = []
    for case in cases:
        item = {"id": str(case.get("id")), "input": str(case.get("input") or "")}
        if case.get("expected") is not None:
            item["expected"] = str(case.get("expected"))
        payload.append(item)
    return payload
