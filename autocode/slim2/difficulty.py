"""导入时自动评估题目难度：easy / medium / hard（2026-11-26）。

纯规则打分，不花模型额度——导入发生在学生侧，很多人根本没配模型 Key，
而难度在确认入库那一刻就要落库（题目列表的"待生成"栏直接显示颜色），
所以走本地启发式：算法信号词 + 数据规模 + 题面篇幅 + 判定方式加权。

判得不必精确：难度只影响学生选题时的预期，不参与任何判定逻辑；
宁可保守（默认往 medium 靠），也不让一道题因为评估失败进不了库。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

#: 归一化档位（存库值）与界面文案
LEVELS = ("easy", "medium", "hard")
NAMES = {"easy": "简单", "medium": "中等", "hard": "困难"}

# 硬信号：出现即大概率需要专门的算法知识（每个词只计一次，命中 +3）
_HARD_WORDS = (
    "动态规划", "状压", "状态压缩", "最短路", "最小生成树", "网络流", "最大流",
    "费用流", "线段树", "树状数组", "平衡树", "后缀数组", "后缀自动机",
    "并查集", "拓扑排序", "强连通", "二分图", "最近公共祖先", "树链剖分",
    "组合数", "博弈", "期望", "概率", "矩阵快速幂", "费马小定理", "欧拉函数",
    "莫比乌斯", "中国剩余", "卢卡斯定理", "多项式", "FFT", "NTT", "斜率优化",
    "决策单调", "哈密顿", "欧拉回路", "Nim", "质因数", "质因子", "大素数",
    "数论", "高精度", "状态机DP",
)

# 中等信号：常见基础算法/数据结构技巧（每个词只计一次，合计上限 +2）
_MEDIUM_WORDS = (
    "贪心", "二分", "双指针", "滑动窗口", "前缀和", "差分", "哈希", "回溯",
    "记忆化", "递归", "KMP", "优先队列", "单调栈", "单调队列", "区间覆盖",
    "搜索",
)

# 简单信号：不需要算法知识的模拟/统计类（每个词只计一次，合计上限 -2）
_EASY_WORDS = (
    "模拟", "枚举", "遍历", "求和", "平均值", "计数", "统计", "签到",
    "四则运算", "按顺序输出", "比较大小", "排序",
)

# 上标数字 → 普通数字（题面经 normalize_math 后 10⁵/10¹⁰ 用上标字形）
_SUP_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

_SCALE_POW = re.compile(r"10\^(\d+)")              # 10^5
_SCALE_SUP = re.compile(r"10([⁰¹²³⁴⁵⁶⁷⁸⁹]+)")       # 10⁵（normalize 后的题面形态）
_SCALE_SCI = re.compile(r"(?<![a-z0-9])(\d)e(\d+)(?![a-z0-9])", re.IGNORECASE)  # 1e5 原文兜底
_SCALE_NUM = re.compile(r"[≤<=]\s*(\d{3,12})")     # n ≤ 100000


def _max_scale(text: str) -> int:
    """题面里最大的数据量级（10 的指数）。抓不到算 0。"""
    best = 0
    for match in _SCALE_POW.finditer(text):
        best = max(best, int(match.group(1)))
    for match in _SCALE_SUP.finditer(text):
        digits = match.group(1).translate(_SUP_DIGITS)
        if digits:
            best = max(best, int(digits))
    for match in _SCALE_SCI.finditer(text):
        best = max(best, int(match.group(2)))
    for match in _SCALE_NUM.finditer(text):
        value = int(match.group(1).replace(",", ""))
        best = max(best, len(str(value)) - 1)
    return best


def _count_words(text: str, words: Sequence[str]) -> int:
    return sum(1 for word in words if word in text)


def estimate(
    statement: str,
    constraints: Sequence[Any] | None = None,
    *,
    judge_type: str = "",
) -> str:
    """题面 → easy / medium / hard。任何输入都不抛异常。"""
    try:
        return _score(statement, constraints or (), judge_type)
    except Exception:  # noqa: BLE001 - 评估失败不拦导入
        return ""


def _score(statement: str, constraints: Sequence[Any], judge_type: str) -> str:
    text = str(statement or "")
    extra = "\n".join(str(item) for item in constraints if str(item).strip())
    blob = f"{text}\n{extra}" if extra else text

    score = 0
    score += 3 * _count_words(blob, _HARD_WORDS)
    score += min(_count_words(blob, _MEDIUM_WORDS), 2)
    score -= min(_count_words(blob, _EASY_WORDS), 2)

    scale = _max_scale(blob)
    if scale >= 9:
        score += 3  # 10⁹ 以上：答案必然大数/取模/数学题
    elif scale >= 5:
        score += 1  # 10⁵ 以上：暴力不可行，但线性做法仍可能很简单
    elif scale >= 3:
        score += 1
    elif scale <= 1:
        score -= 1  # n ≤ 100 量级：暴力可过，通常简单

    if len(text) > 600:
        score += 1
    if len(text) > 1200:
        score += 1

    if str(judge_type or "").strip() in ("special", "interactive"):
        score += 2

    if score >= 4:
        return "hard"
    if score >= 1:
        return "medium"
    return "easy"
