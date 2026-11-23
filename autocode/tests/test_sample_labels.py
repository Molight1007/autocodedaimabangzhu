"""样例标签识别：不同来源的题面写法都要解析出同一组样例。

背景（2026-11-22）：原来只认 CSP 的 `【样例输入】`。学生从洛谷、牛客、LeetCode
粘贴的题面用的是 `样例输入：`、`示例 1 输入：`、`输入样例`、`Sample Input`，
一律解析出 0 组样例；而生成测试数据的入口曾把"有样例"当硬门禁，
症状就是"文本导入的题永远拿不到测试数据"。

这个测试按**写法**逐个钉住，防止以后为了支持某一种来源又把另一种改坏。
"""

from __future__ import annotations

import unittest

from autocode.app.ingest_csp import parse_sample_label, parse_samples


def _samples(text: str):
    return parse_samples(str(text).strip().split("\n"))


class TestLabelForms(unittest.TestCase):
    def test_csp_brackets(self) -> None:
        samples, external, _ = _samples(
            """
【题目描述】给定 n 个数求和。
【样例输入】
3
1 2 3
【样例输出】
6
"""
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].input_text, "3\n1 2 3\n")
        self.assertEqual(samples[0].output_text, "6\n")
        self.assertEqual(external, 0)

    def test_plain_colon_labels(self) -> None:
        samples, _, _ = _samples(
            """
样例输入：
3
1 2 3
样例输出：
6
"""
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].input_text, "3\n1 2 3\n")

    def test_input_sample_order(self) -> None:
        """洛谷写法：`输入样例` 与 `输出样例`（词序相反）。"""
        samples, _, _ = _samples(
            """
输入样例
1 2
输出样例
3
"""
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].input_text, "1 2\n")
        self.assertEqual(samples[0].output_text, "3\n")

    def test_numbered_examples(self) -> None:
        samples, _, _ = _samples(
            """
示例 1 输入：
1 2
示例 1 输出：
3
示例 2 输入：
5 7
示例 2 输出：
12
"""
        )
        self.assertEqual(len(samples), 2)
        self.assertEqual([item.output_text.strip() for item in samples], ["3", "12"])

    def test_marker_then_bare_labels(self) -> None:
        """LeetCode 中文站：`示例 1：` 后面跟 `输入：/输出：`，数据与标签同行。"""
        samples, _, warnings = _samples(
            """
示例 1：
输入：nums = [2,7,11,15], target = 9
输出：[0,1]
解释：因为 nums[0] + nums[1] == 9
示例 2：
输入：nums = [3,2,4], target = 6
输出：[1,2]
"""
        )
        self.assertEqual(len(samples), 2)
        self.assertTrue(samples[0].input_text.startswith("nums = [2,7,11,15]"))
        self.assertEqual(samples[0].output_text.strip(), "[0,1]")
        self.assertEqual(warnings, [])

    def test_sample_marker_without_kind_is_not_external(self) -> None:
        """`【样例 1】` 只是块标题，后面还有输入/输出标签，不能算成"外链样例"。"""
        _, external, warnings = _samples(
            """
【样例 1】
【样例 1 输入】
1 2
【样例 1 输出】
3
"""
        )
        self.assertEqual(external, 0)
        self.assertEqual(warnings, [])

    def test_english_labels(self) -> None:
        samples, _, _ = _samples(
            """
Sample Input
3
1 2 3
Sample Output
6
"""
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].output_text, "6\n")

    def test_inline_data_after_label(self) -> None:
        samples, _, _ = _samples(
            """
样例输入：3
样例输出：3
"""
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].input_text, "3\n")

    def test_repeated_pairs_bump_index(self) -> None:
        """题面不编号、连着写两组输入/输出：后一组不能覆盖前一组。"""
        samples, _, _ = _samples(
            """
输入：
3
输出：
6
输入：
1 2
输出：
3
"""
        )
        self.assertEqual(len(samples), 2)
        self.assertEqual(
            [item.output_text.strip() for item in samples], ["6", "3"]
        )


class TestNoFalsePositive(unittest.TestCase):
    def test_format_sections_are_not_samples(self) -> None:
        """`输入格式`/`输出格式` 是小节标题，不是样例标签。"""
        samples, external, _ = _samples(
            """
【输入格式】
第一行一个整数 n。
【输出格式】
一个整数。
【输入描述】
略。
"""
        )
        self.assertEqual(len(samples), 0)
        self.assertEqual(external, 0)

    def test_markdown_heading_is_not_bare_label(self) -> None:
        """`## 输入` 是 markdown 小节标题，不能当成样例标签。"""
        samples, _, _ = _samples(
            """
## 输入
第一行一个整数 n，表示序列长度。
## 输出
一个整数，表示答案。
"""
        )
        self.assertEqual(len(samples), 0)

    def test_prose_after_bare_label_is_trimmed(self) -> None:
        """裸标签后面是中文说明时，整段按说明文字剔除，不会变成样例数据。"""
        samples, _, warnings = _samples(
            """
输入：
第一行一个整数 n，第二行 n 个整数。
输出：
一个整数，表示最大值。
"""
        )
        self.assertEqual(len(samples), 0)
        self.assertTrue(any("缺输入或输出" in item for item in warnings), warnings)


class TestLabelParser(unittest.TestCase):
    def test_kind_and_index(self) -> None:
        self.assertEqual(parse_sample_label("【样例 2 输出】"), ("输出", 2, ""))
        self.assertEqual(parse_sample_label("输入样例 3："), ("输入", 3, ""))
        self.assertEqual(parse_sample_label("Sample Input 1"), ("输入", 1, ""))
        self.assertEqual(parse_sample_label("示例1输入："), ("输入", 1, ""))

    def test_marker_and_inline(self) -> None:
        self.assertEqual(parse_sample_label("示例 1："), ("", 1, ""))
        self.assertEqual(parse_sample_label("输入：3 5"), ("输入", 0, "3 5"))
        self.assertEqual(parse_sample_label("【样例输入】3 5"), ("输入", 0, "3 5"))

    def test_rejects_non_labels(self) -> None:
        for line in ("输入格式", "输出描述：", "## 输入", "样例解释如下", "时间限制：1s", ""):
            self.assertIsNone(parse_sample_label(line), line)


if __name__ == "__main__":
    unittest.main()
