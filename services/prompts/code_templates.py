from __future__ import annotations

from typing import Any, Dict, Optional


TEMPLATE_GENERATE_DATA = """def generate_data(problem: dict) -> dict:
    \"\"\"生成测试数据。

    返回值 dict 约定（最小要求）：
    - stdin: str                # 必须，整份输入内容，建议以 '\\n' 结尾
    - stdout: str               # 必须保留键，可留空字符串
    - meta: dict                # 可选，元信息，如种子、备注、难度标签等
    - title: str                # 可选，ZIP标题，不写和旧版保持一致

    你可以只返回包含 stdin/stdout 的最小结构，也可以按需要补充 meta。

    警告：
    - 这段代码会在本机运行，请不要粘贴来源不明或恶意代码
    - 谨慎使用 os、subprocess 等对系统有影响的模块
    \"\"\"
    \n
    import random
    \n
    data = {
        "stdin": "",
        "stdout": "",
        "meta": {},
    }
    return data
"""


TEMPLATE_SOLVE = """def solve(problem: dict, data: dict) -> dict:
    \"\"\"解题函数。

    入参：
    - problem: 题目描述及约束信息
    - data: generate_data 返回的 dict，用于还原输入或获取 meta

    返回值 dict 约定：
    - stdout: str                # 必须，作为答案输出的内容（建议以 '\\n' 结尾）
    - meta: dict                 # 可选，调试或统计信息，如耗时、使用策略等

    警告：
    - 解题代码同样会在本机运行，请避免执行高风险系统操作
    \"\"\"
    # 数据提取示例：
    # 1. 获取标准输入文本（如果是标准输入输出题目）
    # stdin_text = data.get("stdin", "")
    
    # 2. 获取变量字典（如果是函数式或需要特定变量）
    # vars_data = data.get("vars", {})
    # n = vars_data.get("n", 0)
    # arr = vars_data.get("arr", [])

    result = {
        "stdout": "",
        "meta": {},
    }
    return result
"""


def get_template(template_name: str, params: Optional[Dict[str, Any]] = None) -> str:
    name = (template_name or "").strip().lower()
    params = params or {}

    if name in {"generate_data", "generate", "data"}:
        return TEMPLATE_GENERATE_DATA

    if name in {"solve", "solution"}:
        return TEMPLATE_SOLVE

    raise ValueError(f"unknown_template:{template_name}")
