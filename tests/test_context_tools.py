import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.isolated_agent.controller.context_tools import build_problem_card, build_user_context


class TestProblemCard(unittest.TestCase):
    def test_problem_card_keeps_core_fields_and_skips_heavy_payloads(self):
        problem = {
            "title": "快乐数",
            "description": "判断一个数是否为快乐数。",
            "constraints": "1 <= n <= 2^31 - 1",
            "stdin": "19\n",
            "stdout": "true\n",
            "cases": [{"stdin": "1\n"}],
            "data_samples": [{"stdin": "19\n", "stdout": "true\n"}],
            "source": "leetcode",
        }

        card = build_problem_card(problem, max_chars=1200)
        self.assertIn("题目卡（固定前缀", card)
        self.assertIn("title:\n快乐数", card)
        self.assertIn("description:\n判断一个数是否为快乐数。", card)
        self.assertIn("constraints:\n1 <= n <= 2^31 - 1", card)
        self.assertIn("other_fields:", card)
        self.assertIn("source", card)
        self.assertNotIn("data_samples", card)
        self.assertNotIn("cases", card)


class TestBuildUserContext(unittest.TestCase):
    def test_later_attempt_still_keeps_problem_card_prefix(self):
        problem = {
            "title": "两数之和",
            "description": "给定两个整数，输出它们的和。",
            "constraints": "1 <= a,b <= 10^9",
        }
        events = [
            {"action": "run", "result_type": "wrong", "version": 2, "note": "run completed", "run_err": "expected 3 got 4"},
            {"action": "read", "result_type": "read_ok", "version": 2, "note": "read:all", "read_file_preview": "X" * 2000},
        ]

        text = build_user_context(
            attempt_idx=5,
            problem=problem,
            session_id="sess-1",
            version=2,
            code_hash="sha256:test",
            events=events,
            max_rounds=15,
            problem_card_max_chars=1200,
            version_snapshots=[{"version": 1, "summary": "initial code"}],
        )

        self.assertIn("题目卡（固定前缀", text)
        self.assertIn("title:\n两数之和", text)
        self.assertIn("动态上下文（可裁剪）", text)
        self.assertIn("attempt=5", text)
        self.assertIn("recent_events:", text)
        self.assertIn("最近一次 read 返回内容（已裁剪）", text)
        self.assertIn("available_versions:", text)
        self.assertNotIn("题目（摘要", text)

    def test_dynamic_context_prefers_latest_events(self):
        problem = {
            "title": "测试题",
            "description": "A" * 200,
        }
        events = []
        for idx in range(1, 13):
            events.append(
                {
                    "action": "run",
                    "result_type": "wrong",
                    "version": idx,
                    "note": f"event-{idx}",
                    "run_err": f"{idx}-" + ("E" * 180),
                }
            )

        text = build_user_context(
            attempt_idx=9,
            problem=problem,
            session_id="sess-2",
            version=12,
            code_hash="sha256:test2",
            events=events,
            max_rounds=12,
            problem_card_max_chars=900,
            version_snapshots=[],
        )

        self.assertIn("v=12  [event-12]", text)
        self.assertIn("v=11  [event-11]", text)
        self.assertNotIn("v=1  [event-1]", text)
        self.assertNotIn("v=2  [event-2]", text)
        self.assertIn("请输出下一步 JSON 动作。", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
