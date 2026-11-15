"""spec 兜底：grammar 表达不了输入格式时，让 LLM 写 parser.py，样例全过才采信。

为什么需要：声明式 grammar 只覆盖 scalar/repeat/matrix/record；
"整行含空格的句子""字符网格"这类格式无法表达，重试多少次都不会变。
LLM 写 Python 解析代码的能力远强于写这套 DSL，因此把"格式理解"交给它，
但**只接受可执行、可复现、样例全过的产物**——不接受任何自然语言解释。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..engine.datagen import input_parser
from ..prompts import loader as prompts
from . import judge
from .prep_rules import replay_samples, statement_excerpt, user_blocks

MAX_PARSER_ATTEMPTS = 2


def parser_fallback(
    service: Any,
    problem: Dict[str, Any],
    base_spec: Optional[Dict[str, Any]],
    samples: List[Dict[str, str]],
    prior_error: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """尝试用 LLM 生成的 parser.py 兜底；成功返回 (spec, detail)，失败返回 (None, 原因)。"""
    if not getattr(service, "allow_parser_fallback", True):
        return None, f"{prior_error}；parser 兜底已关闭"
    if not base_spec or not base_spec.get("input_grammar"):
        return None, f"{prior_error}；parser 兜底跳过：没有可复用的 spec 基础字段"
    feedback = ""
    last_error = ""
    required = sorted(input_parser.required_keys(base_spec))
    if required:
        feedback = (
            "\n\n## 必须输出的字段名\n" + ", ".join(required)
            + "\nJSON 必须包含这些 key（值类型按题面），否则会被判不合格。"
        )
    for attempt in range(1, MAX_PARSER_ATTEMPTS + 1):
        result = service._chat(
            prompts.PARSER,
            user_blocks(statement_excerpt(problem), samples) + feedback,
            tag=f"parser#{attempt}",
            max_tokens=16384,
            json_mode=False,
        )
        code = judge.extract_code(result.content, "python")
        if not code:
            last_error = "未产出 python 代码块"
            feedback = "\n\n上一次没有输出 python 代码块，请只输出一个完整代码块。"
            continue
        spec = dict(base_spec)
        spec["input_parser_code"] = code
        spec["samples"] = samples
        parser_warnings: list[str] = []
        errors = input_parser.check_samples(code, samples, spec, parser_warnings)
        if parser_warnings:
            service._note("parser 质检：" + "；".join(parser_warnings[:2]))
        if not errors:
            errors = replay_samples(spec, samples)
        if errors:
            last_error = "；".join(errors[:3])
            feedback = (
                f"\n\n上一次的 parser.py 在这些样例上失败：{last_error}\n"
                "请修正解析逻辑（注意整行字符串、字符网格、变长字段），重出完整代码。"
            )
            continue
        service.store.save_spec(int(problem["id"]), spec)
        return spec, f"parser.py 兜底：{len(samples)}/{len(samples)} 样例通过"
    return None, f"{prior_error}；parser 兜底失败：{last_error}"
