"""autoCode 确定性引擎包（零 LLM，R2）。

子包：
- sandbox/ 代码沙箱：AST 守卫（agent_guard / 后续 student_guard）与 Python 语法预检
- verify/  判定层：verdict 数据结构（P1 起补 comparator / cross_check）

约束（由 tools/check_layering.py 强制）：
- 禁 import autocode.{llm, agent, app, ui}
- 禁出现 importlib / __import__ / exec / eval 名字（防动态导入绕过）
"""

from __future__ import annotations

__all__: list[str] = []
