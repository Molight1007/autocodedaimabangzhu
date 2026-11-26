"""公式截图与 span 感知抽取的测试。

覆盖三块：
1. `_spanwise_text`：按 span 几何（字号+基线偏移）区分上/下标——
   CSP40 实测同一行里 `x²` 的 2 与 `aᵢ` 的 i 都是 8pt 小字，纯文本分不出来；
2. `repair_pdf_layout` 新规则：∑ 上下限竖排拆行、×10ⁿ、[0, 10ⁿ]；
3. `_math_def_lines` / `_find_formula_bbox` / `attach_math_figures`：
   定义式公式定位与裁剪（用 pymupdf 现场造一份迷你 PDF，无 pymupdf 则跳过）。
"""

from __future__ import annotations

import unittest

from autocode.slim2.segment import (
    _math_def_lines,
    attach_math_figures,
    normalize_statement,
    repair_pdf_layout,
)


class FakeDb:
    """记录 INSERT 的假库：attach_math_figures 只需要 transaction/execute。"""

    def __init__(self) -> None:
        self.inserted: list = []

    def transaction(self):  # noqa: ANN201
        conn = self

        class _Conn:
            def execute(self, sql: str, params=()):  # noqa: ANN001, ANN201
                if "INSERT INTO problem_figures" in sql:
                    self_outer = conn
                    self_outer.inserted.append(params)
                return self

            def __enter__(self):  # noqa: ANN201
                return self

            def __exit__(self, *args):  # noqa: ANN002, ANN201
                return False

        return _Conn()


class SpanwiseTextTest(unittest.TestCase):
    """_spanwise_text：几何判据（小字号 + 基线偏移）→ 上/下标字符。"""

    def _span(self, text: str, size: float, y: float) -> dict:
        return {"text": text, "size": size, "origin": (0.0, y)}

    def test_superscript_raised_small_span(self) -> None:
        # CSP40 p4 实测：2 是 12pt 基线 192.7，3 是 8pt 基线 188.3（抬高 4.4pt）
        from autocode.slim2.files import _spanwise_text

        spans = [self._span(" 2", 12.0, 192.7), self._span("3", 8.0, 188.3)]
        self.assertEqual(_spanwise_text(spans), " 2³")

    def test_subscript_dropped_small_span(self) -> None:
        # CSP40 p2 实测：a 12pt 基线 228.3，i 8pt 基线 230.1（下沉 1.8pt）
        from autocode.slim2.files import _spanwise_text

        spans = [self._span("a", 12.0, 228.3), self._span("i", 8.0, 230.1)]
        self.assertEqual(_spanwise_text(spans), "aᵢ")

    def test_full_size_span_untouched(self) -> None:
        from autocode.slim2.files import _spanwise_text

        spans = [self._span("23", 12.0, 192.7)]
        self.assertEqual(_spanwise_text(spans), "23")

    def test_unmappable_char_falls_back(self) -> None:
        # 大写字母没有上标字形：保守回退原样，绝不产出错记号
        from autocode.slim2.files import _spanwise_text

        spans = [self._span("x", 12.0, 192.7), self._span("Q", 8.0, 188.3)]
        self.assertEqual(_spanwise_text(spans), "xQ")

    def test_empty_line(self) -> None:
        from autocode.slim2.files import _spanwise_text

        self.assertEqual(_spanwise_text([]), "")


class SumSplitRuleTest(unittest.TestCase):
    """∑ 上下限竖排拆行合并（problem 7 实测形态）。"""

    def test_merge_two_lines_with_blank_padding(self) -> None:
        text = "全部测试数据满足：1 ≤n, q, ∑n\ni=1 mᵢ ≤5×105，所有权值值域为[0, 109]。\n"
        fixed = repair_pdf_layout(text)
        self.assertIn("∑ᵢ₌₁ⁿ mᵢ ≤5×10⁵，所有权值值域为[0, 10⁹]。", fixed)
        # 合并两行成一行 + 补一个空行：行数守恒
        self.assertEqual(text.count("\n"), fixed.count("\n"))

    def test_five_consecutive_pairs(self) -> None:
        text = "".join(
            f"• 子任务{i}：1 ≤n, ∑n\ni=1 mᵢ ≤{i}05。\n" for i in range(1, 6)
        )
        fixed = repair_pdf_layout(text)
        self.assertEqual(text.count("\n"), fixed.count("\n"))
        self.assertEqual(fixed.count("∑ᵢ₌₁ⁿ"), 5)
        self.assertNotIn("i=1 ", fixed)

    def test_upper_limit_unmappable_keeps_original(self) -> None:
        # 上标转不出来（不该发生的形态）→ 原样保留，宁缺毋错
        text = "∑\ni=1 mᵢ ≤5。\n"
        self.assertEqual(repair_pdf_layout(text), text)

    def test_times_pow(self) -> None:
        self.assertIn("5×10⁵", repair_pdf_layout("≤5×105，"))
        # 真值不撞：×1010 不是 10¹
        self.assertIn("×1010", repair_pdf_layout("×1010"))

    def test_bracket_pow(self) -> None:
        self.assertIn("[0, 10³]", repair_pdf_layout("值域为[0, 103]。"))
        # 紧凑形态也归一成带空格的写法
        self.assertIn("[0, 10³]", repair_pdf_layout("值域为[0,103]。"))


class MathDefLineTest(unittest.TestCase):
    """定义式公式的题面行识别。"""

    def test_detects_definition_line(self) -> None:
        text = "【题目描述】\n定义\nf(S) = ⊕ᵢ∈S aᵢ\n其中⊕为按位异或运算。\n"
        self.assertEqual(_math_def_lines(text), [(3, "f(S)", "⊕")])

    def test_ignores_inline_and_text_lines(self) -> None:
        text = "若x, k 均为[0, 2³) 内的整数，定义f(x, k) = ((x² + k²) mod 2³) ⊕k。\n"
        self.assertEqual(_math_def_lines(text), [])


class FormulaBBoxTest(unittest.TestCase):
    """_find_formula_bbox / attach_math_figures：用迷你 PDF 做端到端验证。"""

    PDF = None  # 惰性生成

    @classmethod
    def _build_pdf(cls):  # noqa: ANN202
        if cls.PDF is not None:
            return cls.PDF
        try:
            import pymupdf  # type: ignore
        except ImportError:
            return None
        doc = pymupdf.open()
        page = doc.new_page()
        # 正文行
        page.insert_text((72, 100), "定义如下函数，", fontsize=12, fontname="china-s")
        # 竖排公式：锚点行 + 右侧小字行
        page.insert_text((200, 140), "f(S) =", fontsize=12, fontname="china-s")
        page.insert_text((240, 130), "⊕", fontsize=12, fontname="china-s")
        page.insert_text((242, 152), "i∈S", fontsize=8, fontname="china-s")
        page.insert_text((262, 140), "ai", fontsize=12, fontname="china-s")
        # 公式右侧的正文行（不该被并进 bbox）
        page.insert_text((420, 140), "其中⊕为按位异或。", fontsize=12, fontname="china-s")
        cls.PDF = doc
        return doc

    def test_find_formula_bbox(self) -> None:
        doc = self._build_pdf()
        if doc is None:
            self.skipTest("pymupdf 未安装")
        from autocode.slim2.segment import _find_formula_bbox

        found = _find_formula_bbox(doc, "f(S)")
        self.assertIsNotNone(found)
        page, bbox = found
        self.assertEqual(page, 1)
        x0, y0, x1, y1 = bbox
        # bbox 覆盖锚点行与右侧公式部件，但不吞右侧 420pt 起的正文
        self.assertLess(x0, 250)
        self.assertGreater(x1, 260)
        self.assertLess(x1, 415)

    def test_attach_math_figures_end_to_end(self) -> None:
        doc = self._build_pdf()
        if doc is None:
            self.skipTest("pymupdf 未安装")
        import tempfile
        from pathlib import Path

        from autocode.slim2 import config

        statement = "【题目描述】\n定义\nf(S) = ⊕ᵢ∈S aᵢ\n其中⊕为按位异或运算。\n"
        with tempfile.TemporaryDirectory() as tmp:
            cache = config.DATA_DIR / "pdf_cache"
            cache.mkdir(parents=True, exist_ok=True)
            key = "_test_math_fig"
            (cache / f"{key}.pdf").write_bytes(doc.tobytes())
            try:
                db = FakeDb()
                saved = attach_math_figures(db, 999, key, statement)
                self.assertEqual(saved, 1)
                params = db.inserted[0]
                # label / after_line / ordinal
                self.assertEqual(params[1], "math_3")
                self.assertEqual(params[7], 3)
                self.assertEqual(params[8], 903)
                png = Path(str(params[4]).replace("/assets/problem_999/", ""))
                self.assertTrue((config.DATA_DIR / "assets" / "problem_999" / png).is_file())
                (config.DATA_DIR / "assets" / "problem_999" / png).unlink(missing_ok=True)
            finally:
                (cache / f"{key}.pdf").unlink(missing_ok=True)


class LineCountConservationTest(unittest.TestCase):
    """normalize_statement 幂等性抽查：修复过的文本再修一遍不变。"""

    def test_idempotent_on_repaired_text(self) -> None:
        text = "• 子任务一：1 ≤n, ∑n\ni=1 mᵢ ≤10⁵，值域[0, 10³]。\nf(S) = ⊕ᵢ∈S aᵢ\n"
        once = normalize_statement(text)
        twice = normalize_statement(once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
