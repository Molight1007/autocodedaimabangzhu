"""D1R 代码答案门的契约测试。

测的是"证据链"本身，不是模型好坏：
- 题面有样例时，代码必须全过才算 `anchored`；
- 题面没有样例时，代码照样执行，只是 `anchored=False`；
- 裁决只在"执行结果"与"独立复核"之间做：一致 → cross_verified，
  复核不准 → reference_only，复核自信算出不同结果且无样例锚定 → 丢弃。

这里用真实 Python 子进程跑代码（不 mock 执行器）：门的意义就是"真跑"，
若把执行也 mock 掉，这组测试就证明不了任何事。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from autocode.slim2 import reference
from autocode.slim2.llm_stage import StageRunner

SAMPLES = [{"index": 1, "input": "1 2\n", "output": "3\n"}]

GOOD_CODE = """
import sys
data = sys.stdin.buffer.read().split()
a, b = int(data[0]), int(data[1])
print(a + b)
"""

# 过得了样例（1+2=3），但其余输入全错：用来模拟"标程看着对、其实不对"
SAMPLE_ONLY_CODE = """
import sys
data = list(map(int, sys.stdin.buffer.read().split()))
if len(data) == 2 and data[0] == 1:
    print(3)
else:
    print(data[0] - data[1])
"""

BROKEN_CODE = "import sys\nraise SystemExit('boom')\n"


def _runner(code: str) -> StageRunner:
    runner = StageRunner(None, budget=8, responder=lambda *_args: {"code": code})
    runner._record = lambda *a, **kw: None  # 记账与本测试无关
    return runner


def _items() -> list:
    return [
        {"index": 0, "input": "3 5\n", "expected_output": "8\n"},   # 真值 8
        {"index": 1, "input": "10 4\n", "expected_output": "6\n"},  # 真值 14，故意写错
    ]


class TestExtractCode(unittest.TestCase):
    def test_fenced_block(self) -> None:
        text = "import sys\n```python\nprint(1)\n```\n以上。"
        self.assertEqual(reference.extract_code(text), "print(1)")

    def test_unfenced_text(self) -> None:
        self.assertEqual(reference.extract_code("print(1)\nprint(2)"), "print(1)\nprint(2)")

    def test_empty(self) -> None:
        self.assertEqual(reference.extract_code("   "), "")


class TestVerifyByRun(unittest.TestCase):
    def test_agree_when_solution_matches(self) -> None:
        result = reference.verify_by_run(
            _runner(GOOD_CODE), "<题目/>",
            samples=SAMPLES, items=_items(), judge_type="token",
        )
        self.assertTrue(result.engaged, result.reason)
        self.assertEqual((result.samples_passed, result.samples_total), (1, 1))
        self.assertEqual(result.status_of(0).status, reference.AGREE)
        self.assertEqual(result.status_of(1).status, reference.CORRECTED)
        self.assertEqual(result.status_of(1).output.strip(), "14")

    def test_gate_void_when_samples_fail(self) -> None:
        """有样例却过不了 → 代码不可信，整道题不拿它算答案。"""
        result = reference.verify_by_run(
            _runner(SAMPLE_ONLY_CODE + "\n"), "<题目/>",
            samples=[{"index": 1, "input": "5 5\n", "output": "10\n"}],
            items=_items(), judge_type="token",
        )
        self.assertFalse(result.engaged)
        self.assertFalse(result.anchored)
        self.assertIn("样例", result.reason)
        self.assertEqual(result.verdicts, {})

    def test_no_samples_still_executes(self) -> None:
        """没有样例不是作废理由：代码照跑，只是没有锚。"""
        result = reference.verify_by_run(
            _runner(GOOD_CODE), "<题目/>",
            samples=[], items=_items(), judge_type="token",
        )
        self.assertTrue(result.engaged, result.reason)
        self.assertFalse(result.anchored)
        self.assertEqual(result.status_of(0).status, reference.AGREE)
        self.assertEqual(result.status_of(1).status, reference.CORRECTED)

    def test_gate_void_when_solution_crashes(self) -> None:
        result = reference.verify_by_run(
            _runner(BROKEN_CODE), "<题目/>",
            samples=SAMPLES, items=_items(), judge_type="token",
        )
        self.assertFalse(result.engaged)
        self.assertEqual(result.verdicts, {})

    def test_gate_void_when_code_is_blocked(self) -> None:
        """标程里出现 os/subprocess 之类 → 安全守卫拦下，不执行。"""
        result = reference.verify_by_run(
            _runner("import os\nprint(os.getcwd())\n"), "<题目/>",
            samples=SAMPLES, items=_items(), judge_type="token",
        )
        self.assertFalse(result.engaged)
        self.assertIn("安全守卫", result.reason)


class TestDecideCases(unittest.TestCase):
    """两方裁决：执行结果是答案来源，复核是第二证据；无锚定时分歧就丢。"""

    def _decide(self, d3: str, *, anchored: bool = False,
                expected: str = "6\n", d3_output: str | None = None):
        ref = reference.ReferenceResult(engaged=True, anchored=anchored)
        items = [{"index": 0, "input": "10 4\n", "expected_output": expected,
                  "layer": "typical", "size_class": "small", "input_hash": "h"}]
        verdicts = {0: {"verdict": d3, "recomputed_output": d3_output, "reason": ""}}
        accepted, rejected, decisions = reference.decide_cases(
            items, verdicts, ref, "token", ""
        )
        return accepted, rejected, decisions[0], ref

    def test_review_agrees_is_cross_verified(self) -> None:
        accepted, rejected, decision, _ref = self._decide("pass", d3_output="6\n")
        self.assertEqual(decision["action"], "keep")
        self.assertEqual(decision["output"], "6\n")
        self.assertEqual(decision["trust"], "cross_verified")
        self.assertEqual((len(accepted), len(rejected)), (1, 0))

    def test_review_uncertain_is_reference_only(self) -> None:
        """复核算不准，但代码真跑过 → 数据可用，信任等级降为 reference_only。"""
        _accepted, _rejected, decision, _ref = self._decide("uncertain")
        self.assertEqual(decision["action"], "keep")
        self.assertEqual(decision["trust"], "reference_only")

    def test_confident_mismatch_without_anchor_is_rejected(self) -> None:
        """代码与复核各自算出一个数、又没有样例可裁决 → 丢弃，不猜。"""
        accepted, rejected, decision, _ref = self._decide(
            "mismatch", anchored=False, d3_output="5\n"
        )
        self.assertEqual(decision["action"], "reject")
        self.assertIn("没有样例", decision["note"])
        self.assertEqual((len(accepted), len(rejected)), (0, 1))

    def test_confident_mismatch_with_anchor_keeps_execution(self) -> None:
        """代码已过全部题面样例时，以执行结果为准，但不再标成双证据。"""
        accepted, _rejected, decision, ref = self._decide(
            "mismatch", anchored=True, expected="6\n", d3_output="5\n"
        )
        self.assertEqual(decision["action"], "keep")
        self.assertEqual(decision["trust"], "reference_only")
        self.assertEqual(accepted[0]["expected_output"], "6\n")
        self.assertEqual(ref.conflicts_kept, 1)

    def test_invalid_is_rejected_even_if_run_agrees(self) -> None:
        accepted, _rejected, decision, _ref = self._decide("invalid", anchored=True)
        self.assertEqual(decision["action"], "reject")
        self.assertEqual(len(accepted), 0)

    def test_absent_verdict_is_rejected(self) -> None:
        ref = reference.ReferenceResult(engaged=True)
        items = [{"index": 0, "input": "1 1\n", "expected_output": "2\n"}]
        accepted, rejected, decisions = reference.decide_cases(
            items, {0: {"verdict": "", "recomputed_output": None}}, ref, "token", ""
        )
        self.assertEqual(decisions[0]["action"], "reject")
        self.assertEqual((len(accepted), len(rejected)), (0, 1))


class TestNoNetworkLeak(unittest.TestCase):
    def test_runner_not_used_when_guard_blocks(self) -> None:
        """守卫拦下后不应再调用执行器；顺带确认 prepare_program 没被调用。"""
        with patch("autocode.slim2.reference.prepare_program") as prepare:
            result = reference.verify_by_run(
                _runner("import os\nprint(1)\n"), "<题目/>",
                samples=SAMPLES, items=_items(), judge_type="token",
            )
        self.assertFalse(result.engaged)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
