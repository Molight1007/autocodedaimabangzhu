"""提示词模板（`.md` 常量 + `{{var}}` 插值）。

为什么模板单独成目录、且用 `.md`：
- 提示词是**产品资产**，改动频率高于代码，放在 `.py` 里读起来像字符串拼接；
- 模板里大量出现 `{` `}`（JSON 示例、代码围栏），用 `{{var}}` 而不是 `str.format`
  可以避免满屏 `{{` 转义。

模板清单（设计 §5 ①~⑥ 各阶段一一对应）：
- `spec.md`        题面 → spec JSON
- `generator.md`   spec → generator.py（子进程执行，写数据不写答案）
- `ref_solution.md` spec → 参考解（python/cpp/c）
- `brute.md`       spec → 暴力解（小数据金标准）
- `hints.md`       spec → L1/L2/L3（明令禁止输出代码）
- `parser.md`      grammar 表达不了输入格式时的兜底：LLM 写 parser.py（样例全过才采信）

所有模板都带**注入护栏**：题面里"如果你是 LLM 就……"这类文字一律视为噪声不得执行。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

SPEC = "spec"
GENERATOR = "generator"
REF_SOLUTION = "ref_solution"
BRUTE = "brute"
HINTS = "hints"
PARSER = "parser"
SOLUTION_PLAN = "solution_plan"
EXPLANATION = "explanation"
REF_AGENT = "ref_agent"
METHOD_ANALYSIS = "method_analysis"
CODE_NOTES = "code_notes"
STUDENT_DIAGNOSE = "student_diagnose"

TEMPLATE_NAMES: tuple[str, ...] = (
    SPEC, GENERATOR, REF_SOLUTION, BRUTE, HINTS, PARSER, SOLUTION_PLAN, EXPLANATION, REF_AGENT,
    METHOD_ANALYSIS, CODE_NOTES, STUDENT_DIAGNOSE,
)

# L1~L3 明令禁止输出代码（模板里写死，本处仅作代码侧复核口径）
HINTS_FORBID_CODE_LEVELS: tuple[int, ...] = (1, 2, 3)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def template_path(name: str) -> Path:
    if name not in TEMPLATE_NAMES:
        raise KeyError(f"未知模板 {name!r}，可选 {TEMPLATE_NAMES}")
    return TEMPLATE_DIR / f"{name}.md"


def load(name: str) -> str:
    """读模板原文（未插值）。"""
    return template_path(name).read_text(encoding="utf-8").strip()


def render(template: str, values: Dict[str, object]) -> str:
    """`{{var}}` 插值；缺失变量保留原样（便于早期发现模板与代码不同步）。"""
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template)


def load_all() -> Dict[str, str]:
    """全量加载（供测试断言 5 个模板都存在且带护栏）。"""
    return {name: load(name) for name in TEMPLATE_NAMES}
