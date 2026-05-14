"""Agent 会话配置与 Prompt 构建工具。

为 cross_runner 提供：
- 算法约束系统提示词
- 会话标签生成
- 对拍反馈 payload
- 候选代码骨架
"""

from __future__ import annotations

from typing import Any, Dict

from ....prompts.prompt_builders import get_agent_system_prompt

CROSS_FEEDBACK_INSTRUCTION = (
    "【对拍反馈】你的输出与另一份参考代码的输出不一致。\n"
    "注意：对方代码也可能错误。你的任务是判断谁的输出正确，而不是盲目修改。\n\n"
    "你必须严格按以下步骤思考（内部推理，不输出）：\n"
    "1. 仔细阅读题目描述，确定对于给定的输入，正确答案应该是什么？\n"
    "2. 你的输出是否正确？\n"
    "3. 对方输出是否正确？\n"
    "4. 如果你的输出正确，直接输出 finalize，不要做任何修改。\n"
    "5. 如果你的输出错误，分析错误原因，输出 modify 修复代码。\n"
    "6. 如果你无法确定谁正确，输出 finalize（保持原样），让系统裁决。"
)


def build_algorithm_system_prompt(*, algorithm_name: str, algorithm_desc: str) -> str:
    base = get_agent_system_prompt().strip()
    extra = (
        "\n\n"
        "你当前处于一个独立会话。\n"
        "本会话必须使用且仅使用以下算法路线：\n"
        f"- 算法名称：{algorithm_name}\n"
        f"- 算法描述：{algorithm_desc}\n"
        "硬约束：\n"
        "1) 禁止偏离上述算法路线，不要引入替代算法。\n"
        "2) 目标仅为得到可运行且稳定的最终代码：消除语法错误/运行时错误，完成至少一次 self_test(all_stable=true) 后 finalize。\n"
        "3) 输出协议仍必须严格遵守 JSON action 规范。"
    )
    return base + extra


def bootstrap_candidate_code() -> str:
    return (
        "def solve(problem: dict, data: dict) -> dict:\n"
        "    _ = problem\n"
        "    _stdin = str((data or {}).get('stdin') or '')\n"
        "    _ = _stdin\n"
        "    return {'stdout': '', 'meta': {}}\n"
    )


def build_session_tag(*, slot: str, algorithm_name: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(algorithm_name or "algorithm"))
    clean = clean.strip("-_") or "algorithm"
    return f"verify-{slot}-{clean}"


def build_feedback_payload(first_mismatch: Dict[str, Any]) -> Dict[str, str]:
    return {
        "stdin": str(first_mismatch.get("stdin") or ""),
        "out_a": str(first_mismatch.get("out_a") or ""),
        "out_b": str(first_mismatch.get("out_b") or ""),
    }
