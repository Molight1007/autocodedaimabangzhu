"""引擎判定层。

P0 只迁入 verdict 数据结构；`comparator.py`（checker_type 分派）与
`cross_check.py`（对拍重写）在 P1/P2 补齐。
"""

from __future__ import annotations

from .verdict import (
    AlgorithmInfo,
    AnalysisResult,
    VerifyVerdict,
    create_skipped_verdict,
)

__all__ = [
    "AlgorithmInfo",
    "AnalysisResult",
    "VerifyVerdict",
    "create_skipped_verdict",
]
