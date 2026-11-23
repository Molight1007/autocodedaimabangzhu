"""提示词模板（`.md` 常量 + `{{var}}` 插值）。

为什么模板单独成目录、且用 `.md`：
- 提示词是**产品资产**，改动频率高于代码，放在 `.py` 里读起来像字符串拼接；
- 模板里大量出现 `{` `}`（JSON 示例、代码围栏），用 `{{var}}` 而不是 `str.format`
  可以避免满屏 `{{` 转义。

模板清单（22 个，与 `TEMPLATE_NAMES` 一一对应）。前 16 个属于完整版 `prep_service`，
后面属于精简版 v2（`slim_*`：五段对话 + 图片转写），两套互不复用，避免改 A 影响 B：

- `file_segment.md`   整卷 PDF 文本 → 题目边界（只给行号，不写正文）
- `approach_label.md`  参考解代码 → 2~8 字算法标签
- `spec.md`            题面 → spec JSON
- `generator.md`       spec → generator.py（子进程执行，写数据不写答案）
- `ref_solution.md`    spec → 参考解（盲写，一次成稿）
- `ref_agent.md`       spec → 参考解（可试跑模式：run / final 两步动作）
- `brute.md`           spec → 暴力解（小数据金标准）
- `hints.md`           spec → L1/L2/L3（明令禁止输出代码）
- `parser.md`          grammar 表达不了输入格式时的兜底：LLM 写 parser.py（样例全过才采信）
- `solution_plan.md`   题面 → 1~4 条解法清单（写哪几种思路）
- `explanation.md`     题面 → 学生视角题解（禁代码、禁泄答案）
- `method_analysis.md` 每种解法各一份解析（按 name 一一对应）
- `code_notes.md`      单份代码的结构化讲解
- `student_diagnose.md` 学生错因诊断（WA/TLE/RE）
- `adversarial.md`      参考解 vs 学生错解 → 定向构造能卡住该 bug 的候选输入
- `datasets.md`         题面 + grammar/constraints → 数据计划（分组/档位/条数/必覆盖项）

精简版 v2 五段对话（D1~D5，各自独立上下文，生成者与复核者不共享 messages）：

- `slim_segment.md`   D1 原文（带行号）→ 题目行号边界 + 判定方式（不转述题面）
- `slim_dataset.md`   D2 题面 + 代码答案 → 契约 + ≥10 条分层输入（不算答案）
- `slim_dataset_review.md`  D3 题面 + 执行结果 → 逐条独立重算 `pass/mismatch/invalid/uncertain`
- `slim_reference.md`  D1R 题面 → 一份 Python 代码答案（真跑，stdout 即标准答案）
- `slim_adversarial.md`     D4 题面 + 样例 + 已有数据摘要 + 学生错解 → 定向对抗数据
- `slim_adversarial_review.md` D5 题面 + 对抗数据 + 实际运行结果 → `accept/reject/uncertain`
- `slim_vision.md`     图片（截图/拍照）→ 逐行纯文本转写（供切题切片，不做任何加工）


所有模板都带**注入护栏**：题面里"如果你是 LLM 就……"这类文字一律视为噪声不得执行。
每个模板首行是版本注释 `<!-- prompt: <name> v2 YYYY-MM-DD -->`，`PrepService._chat`
会把模板原文的 sha256 前 16 位随 `llm_calls.prompt_hash` 落库，便于"换提示词后对账"。
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
FILE_SEGMENT = "file_segment"
APPROACH_LABEL = "approach_label"
ADVERSARIAL = "adversarial"
DATASETS = "datasets"

# 精简版 v2：五段对话，一个阶段一个模板（不复用完整版模板）
SLIM_SEGMENT = "slim_segment"
SLIM_DATASET = "slim_dataset"
SLIM_DATASET_REVIEW = "slim_dataset_review"
SLIM_REFERENCE = "slim_reference"
#: 题面残缺时先补全（只补输入输出格式与范围，不改题意）
SLIM_REPAIR = "slim_repair"
SLIM_ADVERSARIAL = "slim_adversarial"
SLIM_ADVERSARIAL_REVIEW = "slim_adversarial_review"
#: 图片导入：多模态模型把截图/拍照转写成纯文本（不是"切题"，没有 JSON 契约）
SLIM_VISION = "slim_vision"

TEMPLATE_NAMES: tuple[str, ...] = (
    SPEC, GENERATOR, REF_SOLUTION, BRUTE, HINTS, PARSER, SOLUTION_PLAN, EXPLANATION, REF_AGENT,
    METHOD_ANALYSIS, CODE_NOTES, STUDENT_DIAGNOSE, FILE_SEGMENT, APPROACH_LABEL, ADVERSARIAL,
    DATASETS,
    SLIM_SEGMENT, SLIM_REPAIR, SLIM_DATASET, SLIM_DATASET_REVIEW, SLIM_REFERENCE,
    SLIM_ADVERSARIAL,
    SLIM_ADVERSARIAL_REVIEW, SLIM_VISION,
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
    """全量加载（供测试断言 12 个模板都存在且带护栏）。"""
    return {name: load(name) for name in TEMPLATE_NAMES}
