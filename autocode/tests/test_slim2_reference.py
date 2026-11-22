"""D2R 参考解复算门的契约测试。

测的是"证据链"本身，不是模型好坏：
- 标程过不了题面样例 → 门必须作废，不能拿它去改任何数据；
- 标程过了样例且与期望一致 → `agree`；
- 标程过了样例但与期望不同 → `corrected`，交给三方裁决；
- 三方裁决在"执行 vs 两次口算"分歧时必须丢弃，不做多数决。

这里用真实 Python 子进程跑标程（不 mock 执行器）：门的意义就是"真跑"，
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
        """标程连样例都过不了 → 整道题的门作废，一条数据都不许改。"""
        result = reference.verify_by_run(
            _runner(SAMPLE_ONLY_CODE + "\n"), "<题目/>",
            samples=[{"index": 1, "input": "5 5\n", "output": "10\n"}],
            items=_items(), judge_type="token",
        )
        self.assertFalse(result.engaged)
        self.assertIn("样例", result.reason)
        self.assertEqual(result.verdicts, {})

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
    """三方裁决：执行结果是硬证据，但不做多数决 —— 分歧就丢。"""

    def _decide(self, status: str, run_output: str, d3: str,
                d3_output: str | None = None, expected: str = "6\n"):
        ref = reference.ReferenceResult(engaged=True)
        ref.verdicts[0] = reference.RunVerdict(0, status, run_output)
        items = [{"index": 0, "input": "10 4\n", "expected_output": expected,
                  "layer": "typical", "size_class": "small", "input_hash": "h"}]
        verdicts = {0: {"verdict": d3, "recomputed_output": d3_output, "reason": ""}}
        accepted, rejected, decisions = reference.decide_cases(
            items, verdicts, ref, "token", ""
        )
        return accepted, rejected, decisions[0]

    def test_agree_keeps_expected(self) -> None:
        accepted, rejected, decision = self._decide(reference.AGREE, "6\n", "pass", "6\n")
        self.assertEqual(decision["action"], "keep")
        self.assertEqual(decision["output"], "6\n")
        self.assertEqual(decision["trust"], "cross_verified")
        self.assertEqual((len(accepted), len(rejected)), (1, 0))

    def test_agree_without_d3_is_reference_only(self) -> None:
        """D3 算不准但执行对上 → 数据可用，但信任等级必须降为 reference_only。"""
        _accepted, _rejected, decision = self._decide(reference.AGREE, "6\n", "uncertain")
        self.assertEqual(decision["action"], "keep")
        self.assertEqual(decision["trust"], "reference_only")

    def test_d3_backs_run_then_fix(self) -> None:
        accepted, _rejected, decision = self._decide(
            reference.CORRECTED, "14\n", "mismatch", "14\n"
        )
        self.assertEqual(decision["action"], "fix")
        self.assertEqual(decision["output"], "14\n")
        # 修正后的期望输出必须同步到可入库条目，否则入库还是旧值
        self.assertEqual(accepted[0]["expected_output"], "14\n")

    def test_d3_uncertain_trusts_run(self) -> None:
        _accepted, _rejected, decision = self._decide(
            reference.CORRECTED, "14\n", "uncertain"
        )
        self.assertEqual(decision["action"], "fix")
        self.assertEqual(decision["trust"], "reference_only")

    def test_d3_pass_vs_run_is_conflict(self) -> None:
        """D3 认同口算、执行结果却不同 → 不赌，丢弃。"""
        accepted, rejected, decision = self._decide(
            reference.CORRECTED, "14\n", "pass", "6\n"
        )
        self.assertEqual(decision["action"], "reject")
        self.assertIn("都不同", decision["note"])
        self.assertEqual((len(accepted), len(rejected)), (0, 1))

    def test_invalid_is_rejected_even_if_run_agrees(self) -> None:
        accepted, _rejected, decision = self._decide(reference.AGREE, "6\n", "invalid")
        self.assertEqual(decision["action"], "reject")
        self.assertEqual(len(accepted), 0)

    def test_unverified_falls_back_to_d3(self) -> None:
        _accepted, _rejected, decision = self._decide(reference.UNVERIFIED, "", "pass")
        self.assertEqual(decision["action"], "keep")
        self.assertIn("未经执行", decision["note"])

    def test_absent_verdict_falls_back(self) -> None:
        """整道题的门作废时，退回纯 D3 口径，不能凭空放行。"""
        empty = reference.ReferenceResult(engaged=False)
        items = [{"index": 0, "input": "1 1\n", "expected_output": "2\n"}]
        accepted, rejected, decisions = reference.decide_cases(
            items, {0: {"verdict": "uncertain", "recomputed_output": None}}, empty, "token", ""
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
