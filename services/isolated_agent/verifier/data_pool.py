"""对拍验证数据池过滤层。

核心职责：
- 在对拍验证前过滤 stdin 过长的用例（防止大型输入填满 LLM 上下文）。
- 限制对拍验证用例总数上限（防止验证耗时过长）。
- 过滤后为空时通知调用方降级跳过。

不负责：
- 不做对拍比对本身（由 cross_runner 负责）。
- 不影响 SolveBatchRunner 的批量测试（仅用于交叉验证链路）。

数据流：
- test_cases（全量） → prepare_cross_validation_pool() → PooledCases（过滤后）
  → run_cross_validation() 使用过滤后的池子进行对拍。

使用方式：
    from .data_pool import prepare_cross_validation_pool

    pool = prepare_cross_validation_pool(test_cases)
    if pool.is_empty:
        return degraded_verdict(reason="data_pool_empty")
    first_compare = _compare_codes_on_cases(..., test_cases=pool.cases, ...)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_MAX_STDIN_CHARS = 3000
DEFAULT_MAX_POOL_SIZE = 50


@dataclass
class PooledCases:
    """过滤后的用例池。

    Attributes:
        cases: 过滤并截断后的用例列表。
        is_empty: 过滤后池子是否为空（调用方应降级跳过）。
        total_original: 原始用例总数。
        total_filtered_by_size: 因 stdin 过长被过滤的数量。
        total_dropped_by_cap: 因池子上限被随机丢弃的数量。
        total_retained: 最终保留的数量（= len(cases)）。
    """

    cases: List[Dict[str, str]] = field(default_factory=list)
    total_original: int = 0
    total_filtered_by_size: int = 0
    total_dropped_by_cap: int = 0

    @property
    def is_empty(self) -> bool:
        return len(self.cases) == 0

    @property
    def total_retained(self) -> int:
        return len(self.cases)


def prepare_cross_validation_pool(
    test_cases: list,
    *,
    max_stdin_chars: int = DEFAULT_MAX_STDIN_CHARS,
    max_pool_size: int = DEFAULT_MAX_POOL_SIZE,
    seed: Optional[int] = None,
) -> PooledCases:
    """准备对拍验证用的数据池。

    处理顺序：
    1. 规范化（dict/str → {"stdin": str}）
    2. 按 stdin 长度过滤（超过 max_stdin_chars 的剔除）
    3. 按池子上限随机采样（超过 max_pool_size 时用 random.sample 截断）

    Args:
        test_cases: 原始测试用例列表（dict 或 str）。
        max_stdin_chars: 单条 stdin 最大字符数，超过则剔除。
        max_pool_size: 池子最大条数，超过则随机采样。
        seed: 可选的随机种子（用于测试可复现）。

    Returns:
        PooledCases 数据类，包含过滤后的用例和统计元数据。
    """
    max_chars = max(1, int(max_stdin_chars or DEFAULT_MAX_STDIN_CHARS))
    max_size = max(1, int(max_pool_size or DEFAULT_MAX_POOL_SIZE))

    # 设置随机种子（仅影响本次采样，不污染全局状态）
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random.Random()

    # 步骤 1：规范化
    raw = _normalize_cases(test_cases)
    total_original = len(raw)

    # 步骤 2：按 stdin 长度过滤
    size_filtered: List[Dict[str, str]] = []
    for item in raw:
        stdin_text = str(item.get("stdin") or "")
        if len(stdin_text) <= max_chars:
            size_filtered.append(item)
    total_filtered_by_size = total_original - len(size_filtered)

    if not size_filtered:
        return PooledCases(
            cases=[],
            total_original=total_original,
            total_filtered_by_size=total_filtered_by_size,
            total_dropped_by_cap=0,
        )

    # 步骤 3：按上限随机采样
    if len(size_filtered) <= max_size:
        return PooledCases(
            cases=size_filtered,
            total_original=total_original,
            total_filtered_by_size=total_filtered_by_size,
            total_dropped_by_cap=0,
        )

    sampled = rng.sample(size_filtered, max_size)
    return PooledCases(
        cases=sampled,
        total_original=total_original,
        total_filtered_by_size=total_filtered_by_size,
        total_dropped_by_cap=len(size_filtered) - max_size,
    )


def _normalize_cases(test_cases: list) -> List[Dict[str, str]]:
    """将混合类型的用例列表规范化为统一的 {"stdin": str} 格式。"""
    out: List[Dict[str, str]] = []
    for item in test_cases or []:
        if isinstance(item, dict):
            out.append({"stdin": str(item.get("stdin") or "")})
        elif isinstance(item, str):
            out.append({"stdin": item})
    return out
