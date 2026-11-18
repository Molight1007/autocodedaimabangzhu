"""题解质检（泄答案）误报修复的回归测试。

历史 bug：`_analysis_issues` 用 `answer in text` 做子串匹配，样例输出只有 `9` 时，
任何含 9 的题解都被判"泄露样例答案"（短答案必然误报）。

新口径：仅当样例答案**去空白后 ≥2 行 或 长度 ≥6** 才做子串检查。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from autocode.app.solutions import _analysis_issues, _answer_is_checkable


def _analysis(**overrides):
    base = {
        "plain_question": "给一个数，求它的平方。",
        "thinking_path": ["先看数据范围", "直接算"],
    }
    base.update(overrides)
    return base


class TestAnswerCheckable(unittest.TestCase):
    def test_short_answer_not_checkable(self) -> None:
        self.assertFalse(_answer_is_checkable("9"))
        self.assertFalse(_answer_is_checkable(" 9 \n"))
        self.assertFalse(_answer_is_checkable("12345"))

    def test_long_or_multiline_answer_checkable(self) -> None:
        self.assertTrue(_answer_is_checkable("123456"))
        self.assertTrue(_answer_is_checkable("1\n2\n"))
        self.assertTrue(_answer_is_checkable("  1  2\n3 4  "))

    def test_empty_answer_not_checkable(self) -> None:
        self.assertFalse(_answer_is_checkable("   \n\n"))


class TestAnalysisIssues(unittest.TestCase):
    def test_short_answer_no_false_positive(self) -> None:
        samples = [{"input": "3\n", "output": "9\n"}]
        issues = _analysis_issues(_analysis(plain_question="9 这个数很关键，先看它。"), samples)
        self.assertNotIn("题解泄露样例答案", issues, issues)

    def test_long_answer_leak_reported(self) -> None:
        answer = "1234567"
        samples = [{"input": "3\n", "output": answer + "\n"}]
        issues = _analysis_issues(_analysis(correctness=f"样例输出就是 {answer}"), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_multiline_answer_leak_reported(self) -> None:
        samples = [{"input": "2\n", "output": "7\n13\n"}]
        issues = _analysis_issues(_analysis(correctness="答案是 7\n13"), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_crlf_sample_still_detected(self) -> None:
        """样例是 CRLF、题解是 LF：不做归一化就会漏报（审查 S3）。"""
        samples = [{"input": "2\r\n", "output": "7\r\n13\r\n"}]
        issues = _analysis_issues(_analysis(correctness="答案是 7\n13"), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_trailing_space_in_sample_still_detected(self) -> None:
        samples = [{"input": "2\n", "output": "7  \n13   \n"}]
        issues = _analysis_issues(_analysis(correctness="答案是 7\n13"), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_trailing_space_in_analysis_still_detected(self) -> None:
        samples = [{"input": "2\n", "output": "7\n13\n"}]
        issues = _analysis_issues(_analysis(correctness="答案是 7  \n13   "), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_indented_answer_still_detected(self) -> None:
        """答案被缩进书写（行首空白）也不能漏。"""
        samples = [{"input": "2\n", "output": "7\n13\n"}]
        issues = _analysis_issues(_analysis(correctness="答案是 7\n   13"), samples)
        self.assertIn("题解泄露样例答案", issues)

    def test_other_checks_still_fire(self) -> None:
        samples = [{"input": "3\n", "output": "9\n"}]
        issues = _analysis_issues({"plain_question": "", "thinking_path": []}, samples)
        self.assertIn("缺少 plain_question", issues)
        self.assertIn("缺少 thinking_path", issues)
        issues = _analysis_issues(_analysis(correctness="```python\nprint(1)\n```"), samples)
        self.assertIn("题解包含代码块", issues)


class TestExplainFlagLifecycle(unittest.TestCase):
    """G2：题解落库不能整体覆盖 `quality_flags`，否则会抹掉 enrich 写的标记。"""

    def test_explain_merges_instead_of_overwriting(self) -> None:
        from autocode.app.solutions import explain
        from autocode.llm.client import ChatResult, ChatUsage

        service = MagicMock()
        service.store.db.query_one.return_value = {
            "analysis_json": json.dumps(
                {"quality_flags": ["方法「模拟法」解析缺失"]}, ensure_ascii=False
            )
        }
        service._chat.return_value = ChatResult(
            content="{}", usage=ChatUsage(1, 1, 2), attempts=1, elapsed_ms=1.0,
            model="stub",
            json_obj={
                "plain_question": "给一个数求平方",
                "difficulty": "边界",
                "thinking_path": ["看范围", "直接算"],
                "correctness": "```python\nprint(1)\n```",
                "complexity": "O(1)",
                "pitfalls": [],
                "tags": ["模拟"],
            },
            finish_reason="stop",
        )

        explain(service, {"id": 1, "statement_md": "题面"}, {"samples": []})

        saved = service.store.save_analysis.call_args[0][1]
        self.assertIn("方法「模拟法」解析缺失", saved["quality_flags"], "旧标记不能被覆盖掉")
        self.assertIn("题解包含代码块", saved["quality_flags"], "本次质检问题要合并进去")

    def test_explain_clears_stale_flags_when_clean(self) -> None:
        """N1：干净题解重跑要清掉上一次的题解类标记，但不能误删 enrich 的方法标记。"""
        from autocode.app.solutions import explain
        from autocode.llm.client import ChatResult, ChatUsage

        service = MagicMock()
        service.store.db.query_one.return_value = {
            "analysis_json": json.dumps(
                {"quality_flags": ["题解包含代码块", "方法「模拟法」解析缺失"]},
                ensure_ascii=False,
            )
        }
        service._chat.return_value = ChatResult(
            content="{}", usage=ChatUsage(1, 1, 2), attempts=1, elapsed_ms=1.0,
            model="stub",
            json_obj={
                "plain_question": "给一个数求平方",
                "difficulty": "边界",
                "thinking_path": ["看范围", "直接算"],
                "correctness": "直接相乘即可",
                "complexity": "O(1)",
                "pitfalls": [],
                "tags": ["模拟"],
            },
            finish_reason="stop",
        )

        explain(service, {"id": 1, "statement_md": "题面"}, {"samples": []})

        saved = service.store.save_analysis.call_args[0][1]
        self.assertNotIn("题解包含代码块", saved["quality_flags"], "干净题解不能继续挂旧警示")
        self.assertIn("方法「模拟法」解析缺失", saved["quality_flags"], "其它来源的标记要保留")


if __name__ == "__main__":
    unittest.main()
