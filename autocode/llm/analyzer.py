"""autoCode 验证层算法分析器（阶段一，P0 从 verifier 迁出）。

为什么放在 llm/ 而不是 engine/（设计 §2 明确结论）：
- 本模块含硬编码 LLM 提示词并直接调用 `llm_caller`，放进 engine/ 会违反 R2，
  且靠 import 图门禁抓不到（提示词是字符串，不是 import）。

依赖方向（门禁 layer-llm-import 强制）：
- 本模块只 import `autocode.engine.verify.verdict`（零依赖叶子，纯数据结构 + 词表），
  不 import `autocode.{agent,app,ui}`。VERIFY_* 词表已下沉到该叶子模块，
  由 `agent.contracts.protocol` 反向 re-export 保持旧路径可用。

核心职责：
- 通过一次 LLM 调用，分析题目的算法特征，返回路由决策。
- 输出结构化的 AnalysisResult，供对拍/交叉验证层做路由分支选择。

不负责：
- 不执行代码、不修改会话状态。
- 不做多轮对话，单次调用完成分析。

约束：
- LLM 输出必须是合法 JSON 对象，字段不合规时降级 confidence=low。
- 解析失败不抛出异常，返回 confidence=low 的降级结果，由路由层处理。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from ..engine.verify.verdict import (
    ALLOWED_VERIFY_CONFIDENCES,
    ALLOWED_VERIFY_OUTPUT_TYPES,
    VERIFY_CONFIDENCE_HIGH,
    VERIFY_CONFIDENCE_LOW,
    VERIFY_CONFIDENCE_MEDIUM,
    VERIFY_OUTPUT_ANY,
    VERIFY_OUTPUT_SINGLE,
    AlgorithmInfo,
    AnalysisResult,
)

# LLM 调用器类型：接收 messages 列表，返回字符串（模型输出）
LlmCaller = Callable[[List[Dict[str, str]]], Any]

# ── 分析器系统提示词 ──────────────────────────────────────────────────────────
# 固定内容，不含任何运行时动态信息，仅描述 JSON 协议与字段规则。
_SYSTEM_PROMPT = (
    "你是一个算法竞赛题目分析专家，擅长识别题目的算法解法特征。\n\n"
    "你的任务：分析给定题目，输出一个 JSON 对象（不得输出任何其他文字）。\n\n"
    "JSON 结构要求（严格遵守）：\n"
    "{\n"
    '  "can_brute_force": <bool>,\n'
    '  "max_n_for_brute": <int 或 null>,\n'
    '  "output_type": <"single_value" | "list" | "any_solution">,\n'
    '  "algorithms": [\n'
    '    {"name": "<算法名称>", "description": "<核心思路一句话>"},\n'
    '    {"name": "<算法名称>", "description": "<核心思路一句话>"}\n'
    "  ],\n"
    '  "confidence": <"high" | "medium" | "low">\n'
    "}\n\n"
    "字段规则：\n"
    "1. can_brute_force：若存在可行暴力解（全排列/子集枚举/多重循环等）"
    "且在小规模下可运行，则为 true。\n"
    "2. max_n_for_brute：仅当 can_brute_force=true 时填写建议规模上限"
    "（例如全排列 n<=8，子集枚举 n<=15，双重循环 n<=500）；否则填 null。\n"
    "3. output_type：\n"
    "   - single_value：输出为唯一确定的单一值（最短距离、最大价值、数量等）。\n"
    "   - list：输出为列表，但顺序唯一（排序后的数组、特定遍历序列等）。\n"
    "   - any_solution：输出为任意可行解"
    "（任意一种拓扑序、任意一种构造方案等）。\n"
    "4. algorithms：必须包含至少 2 个思路不同的解法（暴力解 + 优化解，"
    "或两种不同优化解）。若确实只有一种解法，则只写 1 个，"
    '且 confidence 必须为 "low"。\n'
    "5. confidence：\n"
    "   - high：确信有 2 条独立算法路径，分析结论可靠。\n"
    "   - medium：两条路径存在，但有一定不确定性。\n"
    "   - low：不确定算法路径，或两个算法实际思路雷同，或只找到 1 种解法。\n\n"
    "只输出 JSON，不要任何解释、markdown 代码块、注释。"
)


def analyze_problem(
    problem: Dict[str, Any],
    *,
    llm_caller: LlmCaller,
) -> AnalysisResult:
    """分析题目算法路径，返回路由决策。

    Args:
        problem    : 题目字典，来自上层 problem 对象。
        llm_caller : LLM 调用器，接收 messages 列表，返回模型原始输出（str 或可转 str）。

    Returns:
        AnalysisResult，解析失败时返回 confidence=low 的降级结果，不抛出异常。
    """
    messages = _build_analyzer_messages(problem)
    try:
        raw_output = llm_caller(messages)
    except Exception as exc:
        return _fallback_result(raw="", error=f"llm_caller raised: {exc}")

    raw_text = str(raw_output or "").strip()
    return _parse_analysis_response(raw_text)


# ── 内部实现 ────────────────────────────────────────────────────────────────


def _build_analyzer_messages(problem: Dict[str, Any]) -> List[Dict[str, str]]:
    """构造单次分析请求的消息列表。"""
    problem_text = _safe_repr(problem)
    user_content = "题目（dict）：\n" + problem_text + "\n\n请输出分析 JSON。"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_analysis_response(raw: str) -> AnalysisResult:
    """解析 LLM 输出为 AnalysisResult。

    解析策略：
    1. 直接 json.loads。
    2. 找第一个 { 到最后一个 } 提取 JSON 块。
    3. 两种都失败则返回降级结果。
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return _fallback_result(raw=raw, error="json_parse_failed")
    return _build_analysis_result(obj, raw=raw)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从文本中提取 JSON 对象，失败返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None

    # 策略1：直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 策略2：提取首个 { 到末尾 } 之间的内容（处理 LLM 附加了解释文字的情况）
    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right > left:
        chunk = text[left : right + 1]
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return None


def _build_analysis_result(obj: Dict[str, Any], *, raw: str) -> AnalysisResult:
    """从已解析的 dict 构造 AnalysisResult，字段不合规时降级处理。

    降级规则：
    - 字段缺失/类型错误 -> 使用安全默认值，记入 issues。
    - algorithms < 2   -> confidence 强制降为 low。
    - output_type = any_solution + confidence = high -> 降为 medium。
    """
    issues: List[str] = []

    # ── can_brute_force ──────────────────────────────────────────────────────
    can_brute_raw = obj.get("can_brute_force")
    if isinstance(can_brute_raw, bool):
        can_brute_force = can_brute_raw
    elif isinstance(can_brute_raw, str):
        can_brute_force = can_brute_raw.strip().lower() == "true"
    else:
        can_brute_force = False
        issues.append("can_brute_force missing or invalid, defaulted to false")

    # ── max_n_for_brute ──────────────────────────────────────────────────────
    max_n_raw = obj.get("max_n_for_brute")
    if can_brute_force:
        if isinstance(max_n_raw, (int, float)) and max_n_raw is not None:
            max_n_for_brute: Optional[int] = max(1, int(max_n_raw))
        else:
            # can_brute_force=true 但缺少 max_n，给保守默认值
            max_n_for_brute = 8
            issues.append(
                "max_n_for_brute missing while can_brute_force=true, defaulted to 8"
            )
    else:
        max_n_for_brute = None

    # ── output_type ──────────────────────────────────────────────────────────
    output_type_raw = str(obj.get("output_type") or "").strip().lower()
    if output_type_raw in ALLOWED_VERIFY_OUTPUT_TYPES:
        output_type = output_type_raw
    else:
        output_type = VERIFY_OUTPUT_SINGLE
        issues.append(
            f"output_type '{output_type_raw}' invalid, defaulted to single_value"
        )

    # ── algorithms ───────────────────────────────────────────────────────────
    algos_raw = obj.get("algorithms")
    algorithms = _parse_algorithms(algos_raw, issues)

    # ── confidence ───────────────────────────────────────────────────────────
    confidence_raw = str(obj.get("confidence") or "").strip().lower()
    if confidence_raw in ALLOWED_VERIFY_CONFIDENCES:
        confidence = confidence_raw
    else:
        confidence = VERIFY_CONFIDENCE_LOW
        issues.append(f"confidence '{confidence_raw}' invalid, defaulted to low")

    # 强制降级：算法数量不足
    if len(algorithms) < 2 and confidence != VERIFY_CONFIDENCE_LOW:
        confidence = VERIFY_CONFIDENCE_LOW
        issues.append("forced confidence=low: algorithms < 2")

    # 强制降级：构造题的 high 置信度无实际意义（路由层会将其路由到 skip）
    if output_type == VERIFY_OUTPUT_ANY and confidence == VERIFY_CONFIDENCE_HIGH:
        confidence = VERIFY_CONFIDENCE_MEDIUM
        issues.append("confidence downgraded to medium for any_solution output_type")

    return AnalysisResult(
        can_brute_force=can_brute_force,
        max_n_for_brute=max_n_for_brute,
        output_type=output_type,
        algorithms=tuple(algorithms),
        confidence=confidence,
        raw=raw,
    )


def _parse_algorithms(
    algos_raw: Any,
    issues: List[str],
) -> List[AlgorithmInfo]:
    """解析 algorithms 字段，返回 AlgorithmInfo 列表。

    对每条记录做宽容解析：name/description 缺失时使用占位符而非报错。
    """
    if not isinstance(algos_raw, list) or len(algos_raw) == 0:
        issues.append("algorithms missing or empty, using placeholder")
        return [
            AlgorithmInfo(
                name="unknown",
                description="analyzer failed to extract algorithms",
            )
        ]

    result: List[AlgorithmInfo] = []
    for idx, item in enumerate(algos_raw):
        if not isinstance(item, dict):
            issues.append(f"algorithms[{idx}] is not a dict, skipped")
            continue
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        if not name:
            name = f"algorithm_{idx}"
            issues.append(f"algorithms[{idx}].name empty, using placeholder")
        if not description:
            description = "no description provided"
            issues.append(f"algorithms[{idx}].description empty, using placeholder")
        result.append(AlgorithmInfo(name=name, description=description))

    if not result:
        issues.append("all algorithm items invalid, using placeholder")
        return [
            AlgorithmInfo(
                name="unknown",
                description="analyzer failed to extract algorithms",
            )
        ]

    return result


def _fallback_result(*, raw: str, error: str = "") -> AnalysisResult:
    """LLM 调用失败或 JSON 解析彻底失败时的降级结果。

    返回 confidence=low，触发路由层采用 cross_algo 策略（不做 brute force 对拍）。
    两个 placeholder 算法保证 algorithms >= 2，不再触发二次降级。
    """
    return AnalysisResult(
        can_brute_force=False,
        max_n_for_brute=None,
        output_type=VERIFY_OUTPUT_SINGLE,
        algorithms=(
            AlgorithmInfo(name="unknown_a", description="analyzer parse failed"),
            AlgorithmInfo(name="unknown_b", description="analyzer parse failed"),
        ),
        confidence=VERIFY_CONFIDENCE_LOW,
        raw=raw,
    )


def _safe_repr(obj: Any, limit: int = 3000) -> str:
    """将 problem 对象安全转为文本，过长时截断，避免超 token 限制。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = repr(obj)
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + "\n...\n(truncated)\n...\n" + text[-tail:]