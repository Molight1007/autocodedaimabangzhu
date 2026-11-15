"""engine/sandbox/agent_guard 行为测试（不是"能不能 import"，而是"规则还在不在"）。

为什么要有这个文件：
- 原 test_kernel_import 只断言 `callable(check_code_safety)`，
  规则被删空/退化成 `return None` 也照样绿——那是假门禁。
- 这里逐条断言**具体 rule 名**，任何一条规则被削都会红。

口径：agent_guard 是给 **LLM 生成物** 用的严格版（禁 import/input 等）；
学生代码走 P1 的 `engine/sandbox/student_guard.py`（必须放行 input/sys.stdin）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autocode.engine.sandbox.agent_guard import GuardViolation, check_code_safety

NORMAL_SOLUTION = """
def solve(problem, data):
    nums = list(map(int, data["stdin"].split()))
    return {"stdout": str(sum(nums)) + "\\n"}
"""


class TestBlockedRules(unittest.TestCase):
    def assertRule(self, code: str, expected_rule: str) -> GuardViolation:
        hit = check_code_safety(code)
        self.assertIsNotNone(hit, f"{code!r} 未被拦截")
        assert hit is not None  # for type checkers
        self.assertEqual(hit.rule, expected_rule, f"{code!r} -> {hit.rule}:{hit.detail}")
        return hit

    def test_import_os_blocked(self) -> None:
        hit = self.assertRule("import os", "blocked_import")
        self.assertEqual(hit.detail, "os")

    def test_import_nested_module_blocked_by_root(self) -> None:
        self.assertRule("import subprocess.popen", "blocked_import")

    def test_import_from_subprocess_blocked(self) -> None:
        hit = self.assertRule("from subprocess import run", "blocked_import_from")
        self.assertEqual(hit.detail, "subprocess")

    def test_open_call_blocked(self) -> None:
        hit = self.assertRule("open('/etc/passwd')", "blocked_call")
        self.assertEqual(hit.detail, "open")

    def test_dunder_attribute_blocked(self) -> None:
        hit = self.assertRule("().__class__.__bases__", "blocked_dunder_attr")
        # walk 顺序决定命中哪一个 dunder 属性，两者都算越界
        self.assertIn(hit.detail, {"__class__", "__bases__"})
        self.assertTrue(hit.detail.startswith("__"))

    def test_os_attr_call_blocked(self) -> None:
        """不经 import 直接 `os.system(...)`：命中 blocked_attr_call。"""
        hit = self.assertRule("os.system('echo hacked')", "blocked_attr_call")
        self.assertEqual(hit.detail, "os.system")

    def test_import_takes_precedence_over_attr_call(self) -> None:
        hit = self.assertRule("import os\nos.system('echo hacked')", "blocked_import")
        self.assertEqual(hit.detail, "os")


class TestNormalCodePasses(unittest.TestCase):
    def test_normal_solution_not_killed(self) -> None:
        self.assertIsNone(check_code_safety(NORMAL_SOLUTION))

    def test_plain_algorithm_not_killed(self) -> None:
        code = (
            "def f(a, b):\n"
            "    total = 0\n"
            "    for i in range(a):\n"
            "        if i % b == 0:\n"
            "            total += i\n"
            "    return total\n"
        )
        self.assertIsNone(check_code_safety(code))


class TestKnownLimitation(unittest.TestCase):
    @unittest.expectedFailure
    def test_input_should_be_allowed_for_student_code(self) -> None:
        """KNOWN：agent_guard 拦 input()（LLM 产物用）。

        OJ 式学生代码第一行就是 input()，必须放行——这是 P1 `student_guard.py`
        的职责（设计 §6 沙箱双策略）。本断言写成 expectedFailure 而不是删掉，
        是为了让 P1 落地时它会变成 unexpected success 从而提醒更新这里。
        """
        self.assertIsNone(check_code_safety("n = int(input())\nprint(n)\n"))

    def test_input_currently_blocked(self) -> None:
        """记录当前事实：input() 确实被 agent_guard 拦（rule=blocked_call）。"""
        hit = check_code_safety("n = int(input())\n")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.rule, "blocked_call")
        self.assertEqual(hit.detail, "input")


if __name__ == "__main__":
    unittest.main()
