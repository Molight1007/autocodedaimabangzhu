"""对拍（cross_check）：把两份实现逐用例输出按 checker 规则比一遍，产出可入库的证据。

设计 §5 ④ 的口径：
- 期望输出只有"多方独立实现一致"才敢标 `oracle` / `dual_impl`；
- 分歧不投票、不做多数决，直接记 `conflict` 并让上层判失败（宁失败不入库错数据）。

本模块是纯函数：输入是"用例 id → 输出字符串"的字典，不跑进程、不读 DB，
便于单测（真正的执行在 `runner`，编排在 `app.prep_service`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional

from .comparator import CompareResult, compare

SOURCE_ORACLE = "oracle"
SOURCE_DUAL_IMPL = "dual_impl"
SOURCE_REFERENCE = "reference"
SOURCE_SAMPLE = "sample"
SOURCE_HAND = "hand"
ALLOWED_SOURCE_OF_TRUTH: tuple[str, ...] = (
    SOURCE_ORACLE,
    SOURCE_DUAL_IMPL,
    SOURCE_REFERENCE,
    SOURCE_SAMPLE,
    SOURCE_HAND,
)

TRUST_ORACLE = "oracle"
TRUST_CROSS_VERIFIED = "cross_verified"
TRUST_REFERENCE_ONLY = "reference_only"
TRUST_CONFLICT = "conflict"
ALLOWED_TRUST_LEVELS: tuple[str, ...] = (
    TRUST_ORACLE,
    TRUST_CROSS_VERIFIED,
    TRUST_REFERENCE_ONLY,
    TRUST_CONFLICT,
)


@dataclass
class CrossResult:
    """对拍结论。`disagreements` 保留前 N 条定位信息，便于回填失败阶段。"""

    ok: bool
    common_cases: int
    missing_cases: int
    disagreements: List[Dict[str, object]] = field(default_factory=list)
    participants: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": bool(self.ok),
            "common_cases": int(self.common_cases),
            "missing_cases": int(self.missing_cases),
            "disagreements": list(self.disagreements),
            "participants": list(self.participants),
        }


def cross_check(
    *,
    baseline_name: str,
    baseline: Mapping[str, str],
    candidates: Mapping[str, Mapping[str, str]],
    checker_type: str = "exact",
    float_eps: float = 1e-6,
    max_report: int = 5,
) -> CrossResult:
    """把每个 candidate 与 baseline 逐用例比较。

    Args:
        baseline_name: 基准实现名（例如 `ref_py`），仅用于报告。
        baseline     : 用例 id → 输出。
        candidates   : 实现名 → (用例 id → 输出)。
        checker_type : 比较规则（`exact` / `line_trim` / `float_eps`）。
        max_report   : 最多保留多少条分歧细节。

    Returns:
        CrossResult；`missing_cases` 统计 candidate 缺输出的用例数（缺输出即不算一致）。
    """
    participants = [baseline_name] + sorted(candidates.keys())
    case_ids = list(baseline.keys())
    disagreements: List[Dict[str, object]] = []
    missing = 0
    if not case_ids:
        return CrossResult(ok=False, common_cases=0, missing_cases=0, participants=participants)

    for name in sorted(candidates.keys()):
        outputs = candidates[name] or {}
        for case_id in case_ids:
            if case_id not in outputs:
                missing += 1
                continue
            verdict: CompareResult = compare(
                checker_type, baseline[case_id], outputs[case_id], float_eps=float_eps
            )
            if not verdict.ok:
                if len(disagreements) < max_report:
                    disagreements.append(
                        {
                            "case_id": case_id,
                            "candidate": name,
                            "reason": verdict.reason,
                            "detail": verdict.detail,
                        }
                    )
    return CrossResult(
        ok=not disagreements and missing == 0,
        common_cases=len(case_ids),
        missing_cases=missing,
        disagreements=disagreements,
        participants=participants,
    )


def decide_trust_level(
    *,
    layer: str,
    agreeing_impls: int,
    disagreeing_impls: int,
) -> Optional[str]:
    """按层给出 trust_level（写死规则，不做多数决）。

    - 有分歧 → `conflict`（上层必须判失败）。
    - `stress`/`adversarial` 层禁 `oracle`（设计 §5 ④：大数据层不得标 reference/oracle），
      两实现一致才 `dual_impl` 级别；仅一份实现 → `reference_only`。
    - `sample`/`boundary`/`exhaustive` 层：≥2 实现一致 → `oracle`。
    """
    if disagreeing_impls > 0:
        return TRUST_CONFLICT
    layer_norm = (layer or "").strip().lower()
    if layer_norm in ("stress", "adversarial"):
        if agreeing_impls >= 2:
            return TRUST_CROSS_VERIFIED
        return TRUST_REFERENCE_ONLY
    if agreeing_impls >= 2:
        return TRUST_ORACLE
    if agreeing_impls == 1:
        return TRUST_REFERENCE_ONLY
    return TRUST_CONFLICT


def source_of_truth_for(layer: str, *, impls: int) -> str:
    """`test_cases.source_of_truth` 的赋值规则（stress 禁 reference）。"""
    layer_norm = (layer or "").strip().lower()
    if impls >= 2:
        return SOURCE_DUAL_IMPL
    if layer_norm in ("sample",):
        return SOURCE_SAMPLE
    return SOURCE_REFERENCE


def summarize_layers(rows: Iterable[Mapping[str, object]]) -> Dict[str, int]:
    """按 layer 统计条数，供 prepare 报告与验收输出。"""
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("layer") or "?")
        counts[key] = counts.get(key, 0) + 1
    return counts
