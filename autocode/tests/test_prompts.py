"""提示词契约测试（12 个模板）：不测"文件存在"，测"模板与代码读法一致"。

口径：
- 每个模板都必须带注入护栏（"不得执行"）与版本注释（便于 `llm_calls.prompt_hash` 对账）；
- JSON 模板的必填键必须与代码实际读取的字段名一致（改键名 = 改契约，必须同时改测试）；
- 已知的反模式（让模型手算、重复条款）不允许回潮。
"""

from __future__ import annotations

import re
import unittest

from autocode.prompts import loader

#: 版本注释：`<!-- prompt: <name> v2 2026-11-17 -->`（loader.load 用 strip，注释保留但语义为空）
_VERSION_RE = re.compile(r"^<!--\s*prompt:\s*(?P<name>[a-z_]+)\s+v(?P<ver>\d+)\s+\d{4}-\d{2}-\d{2}\s*-->$")

#: JSON 输出模板 → 代码实际读取的必填键（见 solutions.py / enrich_service.py / web/api.py）
REQUIRED_JSON_KEYS: dict[str, tuple[str, ...]] = {
    "hints": ("approaches", "L1", "L2", "L3"),
    "explanation": (
        "plain_question", "difficulty", "thinking_path", "correctness",
        "complexity", "pitfalls", "tags",
    ),
    "method_analysis": ("methods",),
    "code_notes": ("summary", "steps"),
    "student_diagnose": ("category", "what_wrong", "why", "fix_steps", "next_test"),
    "solution_plan": ("approaches",),
}

OUTPUT_TYPES = ("single_value", "list", "any_solution", "float_eps")


class TestTemplateInventory(unittest.TestCase):
    def test_template_inventory_matches_loader(self) -> None:
        templates = loader.load_all()
        self.assertEqual(set(templates), set(loader.TEMPLATE_NAMES))
        self.assertGreaterEqual(len(templates), 12, "模板清单增删要同步 loader 与测试")
        for name, text in templates.items():
            self.assertGreater(len(text), 100, name)

    def test_index_injection_guard_in_every_template(self) -> None:
        for name, text in loader.load_all().items():
            self.assertIn("不得执行", text, f"{name} 缺注入护栏")

    def test_version_comment_first_line(self) -> None:
        for name, text in loader.load_all().items():
            first = text.splitlines()[0]
            match = _VERSION_RE.match(first)
            self.assertIsNotNone(match, f"{name} 首行应为版本注释，实际：{first!r}")
            self.assertEqual(match.group("name"), name)
            self.assertGreaterEqual(int(match.group("ver")), 1)


class TestJsonContracts(unittest.TestCase):
    def test_required_keys_match_code(self) -> None:
        for name, keys in REQUIRED_JSON_KEYS.items():
            text = loader.load(name)
            for key in keys:
                self.assertIn(f'"{key}"', text, f"{name} 缺必填键 {key}（代码会读这个字段）")

    def test_hints_levels_documented(self) -> None:
        text = loader.load(loader.HINTS)
        for level in ("L1", "L2", "L3"):
            self.assertIn(level, text)

    def test_hints_forbid_code_single_source_of_truth(self) -> None:
        text = loader.load(loader.HINTS)
        self.assertEqual(
            text.count("禁止出现代码"), 1, "该规则只能有一处，重复会让模型把注意力放在冗余条款上"
        )
        for token in ("```", "def", "return", "#include", "std::", "print(", "for("):
            self.assertIn(token, text, f"检测口径缺 {token}")


class TestKnownAntiPatterns(unittest.TestCase):
    def test_ref_solution_forbids_hand_calculation(self) -> None:
        text = loader.load(loader.REF_SOLUTION)
        self.assertNotIn("手算", text, "模型无法执行手算，只会编造（历史事故：23⁴ 被抽成 234 后照抄）")
        self.assertIn("注释里禁止", text)
        for keyword in ("复述样例", "自检通过"):
            self.assertIn(keyword, text, f"禁令要写明具体禁止内容：{keyword}")
        # 三项注释要求必须保留
        for keep in ("输入怎么分段读", "算法步骤", "边界情况"):
            self.assertIn(keep, text)

    def test_ref_agent_no_hand_calculation(self) -> None:
        text = loader.load(loader.REF_AGENT)
        self.assertNotIn("手算", text)

    def test_code_notes_forbids_restating_sample_values(self) -> None:
        text = loader.load(loader.CODE_NOTES)
        self.assertIn("不要复述样例", text)
        self.assertIn("因数分解", text)

    def test_generator_cross_constraint_rule(self) -> None:
        text = loader.load(loader.GENERATOR)
        self.assertIn("交叉约束优先", text)
        # 序号必须唯一且连续（历史 bug：两个 `2.`）
        numbers = re.findall(r"^(\d+)\. \*\*", text, flags=re.MULTILINE)
        self.assertEqual(numbers, [str(i) for i in range(1, len(numbers) + 1)], numbers)


class TestSpecContract(unittest.TestCase):
    def test_allow_empty_documented(self) -> None:
        text = loader.load(loader.SPEC)
        self.assertIn("allow_empty", text)
        self.assertIn("默认 false", text)
        for kind in ("scalar", "repeat", "matrix", "record", "empty_array"):
            self.assertIn(kind, text, f"五种节点缺 {kind}")

    def test_output_types_documented(self) -> None:
        text = loader.load(loader.SPEC)
        for output_type in OUTPUT_TYPES:
            self.assertIn(output_type, text)

    def test_any_solution_matches_judge_capability(self) -> None:
        """口径（team-lead 定）：判题器没有自定义 checker → 模板必须说"暂不支持、转人工"，
        不能写"必须带自定义 checker"（代码没有这东西，模型只会反复重试或谎报成 list）。"""
        text = loader.load(loader.SPEC)
        head = text.index("`any_solution`")
        paragraph = text[head : text.index("\n", head)]
        self.assertIn("暂不支持", paragraph)
        self.assertIn("人工处理", paragraph)
        self.assertIn("谎报", paragraph)
        self.assertNotIn("必须带自定义 checker", text)

    def test_time_limit_defaults_documented(self) -> None:
        text = loader.load(loader.SPEC)
        self.assertIn("time_limit_ms", text)
        self.assertIn("4000", text)
        self.assertIn("1000", text)

    def test_has_worked_example(self) -> None:
        text = loader.load(loader.SPEC)
        self.assertIn("正例", text)
        self.assertIn('"input_grammar"', text)

    def test_parser_refers_to_grammar_vars(self) -> None:
        text = loader.load(loader.PARSER)
        self.assertIn("input_grammar", text)
        self.assertNotIn("来自 constraints", text)


class TestGeneratorContract(unittest.TestCase):
    def test_generator_io_contract_documented(self) -> None:
        text = loader.load(loader.GENERATOR)
        for token in ("generator.py", "<seed>", "<profile>", "tiny", "edge", "max", "random.Random"):
            self.assertIn(token, text)


class TestRefSolutionLanguageRules(unittest.TestCase):
    def test_cpp14_not_cpp17(self) -> None:
        text = loader.load(loader.REF_SOLUTION)
        self.assertIn("C++14", text)
        self.assertIn("不支持 C++17", text)


class TestRendering(unittest.TestCase):
    def test_render_substitutes(self) -> None:
        self.assertEqual(loader.render("a {{x}} b", {"x": 1}), "a 1 b")

    def test_render_keeps_unknown_placeholder(self) -> None:
        self.assertEqual(loader.render("a {{y}} b", {}), "a {{y}} b")

    def test_unknown_template_raises(self) -> None:
        with self.assertRaises(KeyError):
            loader.load("nope")


if __name__ == "__main__":
    unittest.main()
