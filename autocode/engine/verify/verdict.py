"""验证层数据结构定义（阶段三）。

本模块只定义数据结构与轻量辅助函数，不包含任何业务执行逻辑。

为什么 VERIFY_* 词表放在这里（而不是 agent/contracts/protocol.py）：
- `llm/analyzer.py` 需要这些常量，但 llm 层不应反向 import agent（门禁规则
  `layer-llm-import`）。把纯词表下沉到 engine/verify（零依赖叶子），
  agent 侧改为 re-export，两边都能用且不产生反向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional

# ── 验证层词表（analyzer / 对拍 / verdict 共用）─────────────────────────────
# 路由策略
VERIFY_STRATEGY_BRUTE: Final[str] = "brute_force"   # 暴力解 vs 优化解 对拍
VERIFY_STRATEGY_CROSS: Final[str] = "cross_algo"    # 两个独立算法路径 对拍
VERIFY_STRATEGY_SKIP: Final[str] = "skip"           # 输出不唯一，跳过对拍

# 算法分析置信度
VERIFY_CONFIDENCE_HIGH: Final[str] = "high"
VERIFY_CONFIDENCE_MEDIUM: Final[str] = "medium"
VERIFY_CONFIDENCE_LOW: Final[str] = "low"
ALLOWED_VERIFY_CONFIDENCES: Final[tuple[str, ...]] = (
    VERIFY_CONFIDENCE_HIGH,
    VERIFY_CONFIDENCE_MEDIUM,
    VERIFY_CONFIDENCE_LOW,
)

# 输出类型
VERIFY_OUTPUT_SINGLE: Final[str] = "single_value"   # 单一确定值
VERIFY_OUTPUT_LIST: Final[str] = "list"             # 顺序唯一的列表
VERIFY_OUTPUT_ANY: Final[str] = "any_solution"      # 任意可行解（构造题）
ALLOWED_VERIFY_OUTPUT_TYPES: Final[tuple[str, ...]] = (
    VERIFY_OUTPUT_SINGLE,
    VERIFY_OUTPUT_LIST,
    VERIFY_OUTPUT_ANY,
)


@dataclass(frozen=True)
class AlgorithmInfo:
    """单条算法信息（供阶段一 analyzer 兼容使用）。"""

    name: str
    description: str

    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class AnalysisResult:
    """算法分析结果（供阶段一 analyzer 兼容使用）。"""

    can_brute_force: bool
    output_type: str
    algorithms: tuple[AlgorithmInfo, ...]
    confidence: str
    max_n_for_brute: Optional[int] = None
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "can_brute_force": self.can_brute_force,
            "output_type": self.output_type,
            "algorithms": [a.to_dict() for a in self.algorithms],
            "confidence": self.confidence,
        }
        if self.max_n_for_brute is not None:
            payload["max_n_for_brute"] = self.max_n_for_brute
        return payload


@dataclass
class VerifyVerdict:
    """阶段二对拍结果统一结构。"""

    verification_passed: bool  # 是否通过双算法对拍验证
    final_code: str  # 推荐最终使用的代码（优先优化代码）
    brute_force_code: str  # 暴力代码（若存在，否则空字符串）
    optimized_code: str  # 优化代码（若存在，否则空字符串）
    strategy_used: str  # "brute_force" / "cross_algo" / "skip"
    skip_reason: str  # 若跳过验证，说明原因（如 "low_confidence"）
    mismatch_cases: List[Dict] = field(default_factory=list)  # 不一致用例
    summary: Dict = field(default_factory=dict)  # 统计信息

    def to_dict(self) -> dict:
        return {
            "verification_passed": bool(self.verification_passed),
            "final_code": str(self.final_code or ""),
            "brute_force_code": str(self.brute_force_code or ""),
            "optimized_code": str(self.optimized_code or ""),
            "strategy_used": str(self.strategy_used or ""),
            "skip_reason": str(self.skip_reason or ""),
            "mismatch_cases": [dict(item or {}) for item in list(self.mismatch_cases or [])],
            "summary": dict(self.summary or {}),
        }


def create_skipped_verdict(
    reason: str,
    final_code: str = "",
    *,
    brute_force_code: str = "",
    optimized_code: str = "",
    mismatch_cases: Optional[List[Dict]] = None,
    summary: Optional[Dict] = None,
) -> VerifyVerdict:
    """快速构造降级（跳过）验证结果。"""
    return VerifyVerdict(
        verification_passed=False,
        final_code=str(final_code or ""),
        brute_force_code=str(brute_force_code or ""),
        optimized_code=str(optimized_code or ""),
        strategy_used="skip",
        skip_reason=str(reason or ""),
        mismatch_cases=[dict(item or {}) for item in list(mismatch_cases or [])],
        summary=dict(summary or {}),
    )
