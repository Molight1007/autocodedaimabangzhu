"""输出比较器：按 `checker_type` 分派，返回可解释的差异位置。

为什么不用 `==`：CCF 题普遍允许多余的空白与行尾空格，逐字节比较会把正确解判 WA；
反过来"忽略所有空白"又会放过"少输出一行但是内容拼起来一样"错解。因此分三档：

- `exact_tokens`：token 序列完全一致（空白/换行差异不计，行尾空白天然忽略）。
- `line_trim`：逐行 rstrip 后比较，行数也必须一致（对"输出必须分行"的题更严）。
- `float_eps`：token 数一致 + 逐 token 按 `abs(a-b) <= eps or rel <= eps` 比较。

所有比较器都是纯函数，不读文件、不跑进程。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

CHECKER_EXACT = "exact"
CHECKER_LINE_TRIM = "line_trim"
CHECKER_FLOAT_EPS = "float_eps"
ALLOWED_CHECKERS: tuple[str, ...] = (CHECKER_EXACT, CHECKER_LINE_TRIM, CHECKER_FLOAT_EPS)

DEFAULT_FLOAT_EPS = 1e-6
# 差异上下文：报告首个分歧位置前后多少个 token，便于 UI 展示最小反例
CONTEXT_TOKENS = 6


@dataclass
class CompareResult:
    """比较结论。`ok=False` 时 `reason` 说明差在哪一类，`detail` 给出定位。"""

    ok: bool
    reason: str = ""
    checker: str = CHECKER_EXACT
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "reason": self.reason,
            "checker": self.checker,
            "detail": dict(self.detail),
        }


def normalize_newline(text: str) -> str:
    """统一换行并去掉行尾空白；用于展示与 `line_trim`。"""
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n"))


def tokenize(text: str) -> List[str]:
    """按空白切 token（行尾空白/多余空行天然被忽略）。"""
    return (text or "").replace("\r\n", "\n").split()


def _tail_trimmed_lines(text: str) -> List[str]:
    lines = normalize_newline(text).split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def compare_exact_tokens(expected: str, actual: str) -> CompareResult:
    exp_tokens = tokenize(expected)
    act_tokens = tokenize(actual)
    if exp_tokens == act_tokens:
        return CompareResult(ok=True, checker=CHECKER_EXACT, detail={"tokens": len(exp_tokens)})
    first = _first_diff(exp_tokens, act_tokens)
    return CompareResult(
        ok=False,
        reason="token_mismatch",
        checker=CHECKER_EXACT,
        detail={
            "first_diff_index": first,
            "expected_context": exp_tokens[max(0, first - CONTEXT_TOKENS) : first + CONTEXT_TOKENS],
            "actual_context": act_tokens[max(0, first - CONTEXT_TOKENS) : first + CONTEXT_TOKENS],
            "expected_tokens": len(exp_tokens),
            "actual_tokens": len(act_tokens),
        },
    )


def compare_line_trim(expected: str, actual: str) -> CompareResult:
    exp_lines = _tail_trimmed_lines(expected)
    act_lines = _tail_trimmed_lines(actual)
    if exp_lines == act_lines:
        return CompareResult(ok=True, checker=CHECKER_LINE_TRIM, detail={"lines": len(exp_lines)})
    first = _first_diff(exp_lines, act_lines)
    return CompareResult(
        ok=False,
        reason="line_mismatch",
        checker=CHECKER_LINE_TRIM,
        detail={
            "first_diff_line": first + 1,
            "expected_line": exp_lines[first] if first < len(exp_lines) else "<EOF>",
            "actual_line": act_lines[first] if first < len(act_lines) else "<EOF>",
            "expected_lines": len(exp_lines),
            "actual_lines": len(act_lines),
        },
    )


def compare_float_eps(
    expected: str, actual: str, *, eps: float = DEFAULT_FLOAT_EPS
) -> CompareResult:
    exp_tokens = tokenize(expected)
    act_tokens = tokenize(actual)
    if len(exp_tokens) != len(act_tokens):
        return CompareResult(
            ok=False,
            reason="token_count_mismatch",
            checker=CHECKER_FLOAT_EPS,
            detail={"expected_tokens": len(exp_tokens), "actual_tokens": len(act_tokens)},
        )
    for index, (exp_token, act_token) in enumerate(zip(exp_tokens, act_tokens)):
        try:
            exp_value = float(exp_token)
            act_value = float(act_token)
        except ValueError:
            if exp_token != act_token:
                return CompareResult(
                    ok=False,
                    reason="non_numeric_mismatch",
                    checker=CHECKER_FLOAT_EPS,
                    detail={"token_index": index, "expected": exp_token, "actual": act_token},
                )
            continue
        if not _close(exp_value, act_value, eps):
            return CompareResult(
                ok=False,
                reason="float_mismatch",
                checker=CHECKER_FLOAT_EPS,
                detail={
                    "token_index": index,
                    "expected": exp_token,
                    "actual": act_token,
                    "abs_diff": abs(exp_value - act_value),
                    "eps": eps,
                },
            )
    return CompareResult(ok=True, checker=CHECKER_FLOAT_EPS, detail={"tokens": len(exp_tokens), "eps": eps})


def _close(expected: float, actual: float, eps: float) -> bool:
    if math.isnan(expected) or math.isnan(actual):
        return False
    if math.isinf(expected) or math.isinf(actual):
        return expected == actual
    diff = abs(expected - actual)
    if diff <= eps:
        return True
    scale = max(abs(expected), abs(actual), 1.0)
    return diff / scale <= eps


def _first_diff(expected: Sequence[str], actual: Sequence[str]) -> int:
    limit = min(len(expected), len(actual))
    for index in range(limit):
        if expected[index] != actual[index]:
            return index
    return limit


def compare(
    checker_type: str,
    expected: Optional[str],
    actual: str,
    *,
    float_eps: float = DEFAULT_FLOAT_EPS,
) -> CompareResult:
    """按 checker_type 分派；未知 checker 直接抛 ValueError（配置错误必须响）。"""
    checker = (checker_type or "").strip().lower()
    if checker not in ALLOWED_CHECKERS:
        raise ValueError(f"未知 checker_type {checker_type!r}，可选 {ALLOWED_CHECKERS}")
    if expected is None:
        return CompareResult(ok=False, reason="expected_output_missing", checker=checker)
    if checker == CHECKER_LINE_TRIM:
        return compare_line_trim(expected, actual)
    if checker == CHECKER_FLOAT_EPS:
        return compare_float_eps(expected, actual, eps=float_eps)
    return compare_exact_tokens(expected, actual)
