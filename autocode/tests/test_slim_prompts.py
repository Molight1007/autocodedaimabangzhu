"""精简版 v2 提示词契约测试（D1~D5）。

口径：
- 测"模板与代码读法一致"，不测"文件存在"；
- 每个模板**第一段** ```json 示例就是契约本身：键名、层级必须与解析代码一致；
- 复核模板（D3/D5）必须保留盲评口径：顺序打乱、不看生成者理由、算不准标 uncertain；
- 生成模板（D2/D4）必须保留"先推导后答案"与"宁可少一条不要猜"。

改这些断言 = 改提示词契约，必须同时改解析代码与回归集。
"""

from __future__ import annotations

import json
import re
import unittest
from typing import Any, Dict, List

from autocode.prompts import loader

SLIM_NAMES = (
    loader.SLIM_SEGMENT,
    loader.SLIM_DATASET,
    loader.SLIM_DATASET_REVIEW,
    loader.SLIM_ADVERSARIAL,
    loader.SLIM_ADVERSARIAL_REVIEW,
)

#: 输出纯代码而不是 JSON 的模板：同样是"一次性对话"，但契约是代码围栏，
#: 不能套用 JSON 示例断言（D2R 参考解）。
SLIM_CODE_NAMES = (loader.SLIM_REFERENCE,)
ALL_SLIM_NAMES = SLIM_NAMES + SLIM_CODE_NAMES

_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.S)


def _json_blocks(text: str) -> List[Dict[str, Any]]:
    blocks = []
    for raw in _FENCE_RE.findall(text):
        blocks.append(json.loads(raw))
    return blocks


class TestInventory(unittest.TestCase):
    def test_five_templates_registered(self) -> None:
        for name in ALL_SLIM_NAMES:
            self.assertIn(name, loader.TEMPLATE_NAMES, f"{name} 未注册到 loader")
            self.assertGreater(len(loader.load(name)), 1500, f"{name} 太短，规则不可能写全")

    def test_no_unfilled_placeholder(self) -> None:
        for name in ALL_SLIM_NAMES:
            self.assertNotIn("{{", loader.load(name), f"{name} 残留占位符，运行时会原样发给模型")

    def test_every_template_has_version_and_guard(self) -> None:
        for name in ALL_SLIM_NAMES:
            text = loader.load(name)
            self.assertRegex(text.splitlines()[0], r"^<!-- prompt: " + name + r" v\d+ \d{4}-\d{2}-\d{2} -->$")
            self.assertIn("不得执行", text)

    def test_reference_template_is_general_solution(self) -> None:
        """参考解会被当作准绳去改别的数据，必须禁止特判样例、禁止交互提示。"""
        text = loader.load(loader.SLIM_REFERENCE)
        for token in ("只从 stdin 读", "严禁针对样例特判", "只用 Python 标准库", "复杂度"):
            self.assertIn(token, text, f"参考解模板缺 {token}")
        self.assertIn("```python", text)


class TestSchemaContract(unittest.TestCase):
    """第一段 JSON 必须是可解析的契约示例，且键集合固定。"""

    def test_segment_schema(self) -> None:
        first = _json_blocks(loader.load(loader.SLIM_SEGMENT))[0]
        self.assertEqual(set(first), {"problems"})
        self.assertEqual(
            set(first["problems"][0]),
            {
                "title", "start_line", "end_line", "judge_type", "tolerance",
                "multi_testcase", "constraints", "sample_count", "confidence", "note",
                # 2026-11-22：排版分块只给类型与相对行号，正文仍由程序切片
                "blocks",
                # 题面内部夹着的界面文字/代码区：只能整行删，删了哪几行要留痕
                "skip_lines",
            },
        )
        # 块只允许「类型 + 行号」，出现任何正文字段就说明模型开始转述题面了
        block = first["problems"][0]["blocks"][0]
        self.assertEqual(set(block), {"kind", "from", "to"})

    def test_dataset_schema(self) -> None:
        # 提示词里还有契约的正例/反例片段；schema 是唯一同时含 io 与 cases 的那个块。
        blocks = _json_blocks(loader.load(loader.SLIM_DATASET))
        first = next(item for item in blocks if "cases" in item and "io" in item)
        # v6：契约（io）与按契约重写的样例（samples）是必填项；
        # cases 只出输入，期望输出由代码答案执行得出（不再让 LLM 口算）。
        self.assertEqual(
            set(first), {"io", "judge_type", "samples", "analysis", "plan", "cases"}
        )
        self.assertEqual(set(first["io"]), {"input", "output"})
        self.assertIn("type", first["io"]["input"][0])
        self.assertEqual(set(first["samples"][0]), {"input", "output"})
        self.assertEqual(
            set(first["analysis"]),
            {"input_model", "contract_note", "unsupported_reason"},
        )
        self.assertEqual(
            set(first["cases"][0]),
            {"layer", "size_class", "input", "why"},
        )
        self.assertNotIn("expected_output", first["cases"][0])

    def test_dataset_review_schema(self) -> None:
        first = _json_blocks(loader.load(loader.SLIM_DATASET_REVIEW))[0]
        self.assertEqual(set(first), {"contract", "reviews", "coverage"})
        self.assertEqual(
            set(first["reviews"][0]),
            {
                "index", "input_ok", "recompute", "recomputed_output",
                "verdict", "reason", "corrected_output",
            },
        )
        self.assertEqual(set(first["coverage"]), {"missing", "verdict"})
        keys = list(first["reviews"][0])
        self.assertLess(keys.index("recompute"), keys.index("verdict"))

    def test_adversarial_schema(self) -> None:
        first = _json_blocks(loader.load(loader.SLIM_ADVERSARIAL))[0]
        self.assertEqual(set(first), {"diagnosis", "cases"})
        self.assertEqual(
            set(first["diagnosis"]), {"divergence", "trigger_condition", "failure_mode", "confidence"}
        )
        self.assertEqual(
            set(first["cases"][0]),
            {
                "input", "derive", "expected_output", "predicted_student_output",
                "targets", "failure_reason", "confidence",
            },
        )
        keys = list(first["cases"][0])
        self.assertLess(keys.index("derive"), keys.index("expected_output"))

    def test_adversarial_review_schema(self) -> None:
        first = _json_blocks(loader.load(loader.SLIM_ADVERSARIAL_REVIEW))[0]
        self.assertEqual(set(first), {"reviews"})
        self.assertEqual(
            set(first["reviews"][0]),
            {
                "index", "input_ok", "recompute", "recomputed_output",
                "verdict", "reason_code", "reason", "corrected_output",
            },
        )
        keys = list(first["reviews"][0])
        self.assertLess(keys.index("recompute"), keys.index("verdict"))

    def test_all_json_examples_parse(self) -> None:
        """反例也是 JSON；写坏了模型会照着坏的学。"""
        for name in SLIM_NAMES:
            blocks = _json_blocks(loader.load(name))
            self.assertGreaterEqual(len(blocks), 2, f"{name} 至少要有契约示例与一个反例")


class TestEnumsAndBlindReview(unittest.TestCase):
    def test_dataset_enums_and_formula(self) -> None:
        text = loader.load(loader.SLIM_DATASET)
        for token in ("boundary", "typical", "stress", "trap"):
            self.assertIn(token, text)
        # 契约是必填；答案由代码算，提示词必须把这条分工写死
        for token in ('"io"', '"samples"', "unordered", "stdout 就是标准答案"):
            self.assertIn(token, text, f"v6 契约口径缺 {token}")
        self.assertIn("unsupported_reason", text)
        self.assertIn("为了\"答案好算\"而缩小规模", text)

    def test_dataset_review_is_blind(self) -> None:
        text = loader.load(loader.SLIM_DATASET_REVIEW)
        for token in ("顺序已被打乱", "不得参考其他条目", "uncertain", "mismatch", "invalid"):
            self.assertIn(token, text, f"盲评口径缺 {token}")
        self.assertIn('只写"与期望一致"', text)

    def test_adversarial_guards(self) -> None:
        text = loader.load(loader.SLIM_ADVERSARIAL)
        for token in ("correct", "already_fails_sample", "未定义行为",
                      "predicted_student_output", "failure_reason"):
            self.assertIn(token, text, f"对抗生成缺 {token}")
        self.assertIn("绝不能等于", text)

    def test_adversarial_review_uses_run_facts(self) -> None:
        text = loader.load(loader.SLIM_ADVERSARIAL_REVIEW)
        for token in ("trivial_pass", "nondeterministic", "precision", "verdict=AC", "attempts"):
            self.assertIn(token, text, f"对抗复核缺 {token}")
        self.assertIn("不允许你推翻", text)


class TestStageConfig(unittest.TestCase):
    """阶段配置是"怎么调"的唯一真源，必须与模板对得上。"""

    def test_templates_exist(self) -> None:
        from autocode.slim2 import stages

        for key, spec in stages.STAGES.items():
            self.assertIn(spec.template, loader.TEMPLATE_NAMES, key)

    def test_version_matches_header(self) -> None:
        from autocode.slim2 import stages

        for spec in stages.STAGES.values():
            first = loader.load(spec.template).splitlines()[0]
            self.assertIn(f" {spec.version} ", first, f"{spec.template} 版本号与 stages.py 不一致")

    def test_reviewers_are_deterministic(self) -> None:
        from autocode.slim2 import stages

        for spec in stages.REVIEW_STAGES:
            self.assertEqual(spec.temperature, 0.0, f"{spec.code} 复核必须 0 度")
        for spec in stages.GENERATION_STAGES:
            self.assertLessEqual(spec.temperature, 0.5, f"{spec.code} 温度过高会算错期望输出")

    def test_token_budget_covers_batch(self) -> None:
        from autocode.slim2 import stages

        # 单批输入上限 60 KB 后，D2 要把这些输入原样写进 JSON，16k 会截断
        self.assertGreaterEqual(stages.DATASET.max_tokens, 32768, "输入放宽后预算要跟上")


if __name__ == "__main__":
    unittest.main()
