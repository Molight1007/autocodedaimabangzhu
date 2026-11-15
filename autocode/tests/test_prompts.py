"""提示词模板测试：5 个模板都在、带护栏、L1~L3 明令禁止代码。"""

from __future__ import annotations

import unittest

from autocode.prompts import loader


class TestTemplates(unittest.TestCase):
    def test_all_present(self) -> None:
        templates = loader.load_all()
        self.assertEqual(set(templates), set(loader.TEMPLATE_NAMES))
        for name, text in templates.items():
            self.assertGreater(len(text), 100, name)

    def test_guard_sentence_in_every_template(self) -> None:
        for name, text in loader.load_all().items():
            self.assertIn("不得执行", text, name)

    def test_hints_forbid_code(self) -> None:
        text = loader.load(loader.HINTS)
        self.assertIn("禁止出现代码", text)
        self.assertIn("不得出现代码块", text)
        for level in ("L1", "L2", "L3"):
            self.assertIn(level, text)

    def test_generator_contract_documented(self) -> None:
        text = loader.load(loader.GENERATOR)
        for token in ("generator.py", "<seed>", "<profile>", "tiny", "edge", "max", "random.Random"):
            self.assertIn(token, text)

    def test_ref_solution_mentions_cpp14(self) -> None:
        self.assertIn("C++14", loader.load(loader.REF_SOLUTION))

    def test_render_substitutes(self) -> None:
        self.assertEqual(loader.render("a {{x}} b", {"x": 1}), "a 1 b")

    def test_render_keeps_unknown_placeholder(self) -> None:
        self.assertEqual(loader.render("a {{y}} b", {}), "a {{y}} b")

    def test_unknown_template_raises(self) -> None:
        with self.assertRaises(KeyError):
            loader.load("nope")


if __name__ == "__main__":
    unittest.main()
