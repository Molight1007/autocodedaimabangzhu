"""Prompt 组装规则层。

核心职责：
- 将 DeepSeek 精炼请求的 prompt 组装逻辑从 UI 层剥离，集中管理。
- 提供纯函数接口，无副作用，可独立测试。

不负责：
- 不做 UI 校验（题目长度检查等由调用方负责）。
- 不做网络请求或会话管理。
- 不处理上下文截断（由 ChatSession 负责）。

主要数据流：
1. 入口：build_refine_prompt 接收各输入片段 + 上下文状态，返回完整 prompt 字符串。
2. 评分范围：parse_score_range 解析用户输入的最低/最高分，返回标准化元组或 None。
3. 输出：拼接后的 prompt 直接传给 ChatSession.preview_with_user。

约束：
- 评分范围解析异常时静默返回 None，不抛出。
- generate_data 任务专属的评分注入逻辑仅在该 task_name 下生效。
"""

from __future__ import annotations

from typing import Optional, Tuple


def parse_score_range(
    score_min_text: str,
    score_max_text: str,
) -> Optional[Tuple[int, int]]:
    """解析评分范围输入，返回 (最低分, 最高分) 或 None。

    规则：
    - 两个输入均为空时返回 None。
    - 仅填一个时，最低分和最高分取相同值。
    - 最低分 > 最高分时自动交换。
    - 非整数输入静默返回 None。
    """
    s_min = str(score_min_text or "").strip()
    s_max = str(score_max_text or "").strip()
    if not s_min and not s_max:
        return None
    try:
        mn = int(s_min) if s_min else 0
        mx = int(s_max) if s_max else mn
        if mx < mn:
            mn, mx = mx, mn
        return (mn, mx)
    except (ValueError, TypeError):
        return None


def build_refine_prompt(
    *,
    base_prompt: str,
    user_problem: str,
    user_extra: str,
    score_range: Optional[Tuple[int, int]],
    has_history: bool,
    task_name: str,
) -> str:
    """组装完整的精炼请求 prompt。

    数据流：
    1. 首轮（无历史）时以 base_prompt 为起始；后续轮次仅追加用户补充。
    2. 依次拼接：题目描述/约束 → 补充要求 → 评分范围（仅 generate_data）。
    3. 有历史时追加"继续修正"指令，避免模型重置已有实现。

    约束：
    - 各片段之间用双换行分隔，首个非空片段前不加多余换行。
    - score_range 为 None 时跳过评分注入。
    - task_name 非 generate_data 时跳过评分注入，即使 score_range 非 None。
    """
    prompt = ""

    if not has_history:
        prompt = base_prompt

    if user_problem:
        if task_name == "generate_data":
            problem_label = "数据生成约束（用户补充，仅用于数据生成，非解题）：\n"
        elif task_name == "solve":
            problem_label = "解题约束（用户补充）：\n"
        else:
            problem_label = "题目描述/约束（用户补充）：\n"
        prompt += ("\n\n" if prompt else "") + problem_label + user_problem

    if user_extra:
        if task_name == "generate_data":
            extra_label = "数据生成补充要求（用户补充）：\n"
        elif task_name == "solve":
            extra_label = "解题补充要求（用户补充）：\n"
        else:
            extra_label = "补充要求（用户补充）：\n"
        prompt += ("\n\n" if prompt else "") + extra_label + user_extra

    if task_name == "generate_data" and score_range is not None:
        mn, mx = score_range
        prompt += (
            ("\n\n" if prompt else "")
            + "评分范围（用户指定）："
            + f"[{mn}, {mx}]"
            + "\n请在返回的 meta 中加入 score（整数），仅评估本次生成的数据难度。"
        )

    if has_history:
        prompt = (
            (prompt + "\n\n") if prompt else ""
        ) + "请基于之前对话继续修正，不要重置已有变量与整体实现结构，仅做必要改动。"

    return prompt
