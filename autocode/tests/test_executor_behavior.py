"""agent/runtime/executor 真实行为测试（起真子进程，不是 mock）。

为什么要有这个文件：
- 执行器是"判对错"的物理落点，规则退化了（比如判什么都是 right）单元测试必须红。
- 只断言 type 不够，还要确认 right 真的携带 stdout、wrong 真的不携带通过标记。

口径与已知缺口（P1 重写执行器，见 executor 模块 docstring）：
- 现实现用环境变量 `ISOLATED_AGENT_CASE` 传 stdin，且要求代码提供 `solve(problem, data)`；
  **OJ 式 input()/sys.stdin 不可用**，这两条写成 expectedFailure（P1 落地后会变
  unexpected success，从而提醒更新本文件）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autocode.agent.runtime.executor import build_default_run_executor
from autocode.agent.session.state import AgentSessionState

SUM_SOLVE = (
    "def solve(problem, data):\n"
    '    nums = list(map(int, data["stdin"].split()))\n'
    '    return {"stdout": str(sum(nums)) + "\\n"}\n'
)
DIV_ZERO = "def solve(problem, data):\n    return 1/0\n"
INFINITE_LOOP = "def solve(problem, data):\n    while True:\n        pass\n"
OJ_INPUT = (
    "def solve(problem, data):\n"
    "    n = int(input())\n"
    "    return {'stdout': str(n)}\n"
)
OJ_STDIN_READ = (
    "import sys\n"
    "def solve(problem, data):\n"
    "    nums = sys.stdin.read().split()\n"
    "    return {'stdout': str(sum(int(x) for x in nums))}\n"
)


class ExecutorCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例新建一次：工厂返回闭包，创建成本可忽略；
        # 注意不能挂成类属性——函数会成为描述符，被自动绑定 self。
        self.executor = build_default_run_executor(timeout_sec=5.0)

    def run_case(self, code: str, *, stdin: str = "", expected: str | None = None, timeout_sec: float = 5.0):
        context = {"stdin": stdin, "timeout_sec": timeout_sec}
        if expected is not None:
            context["expected_stdout"] = expected
        state = AgentSessionState(session_id=self.id(), current_code=code, runtime_context=context)
        return self.executor(code, state)


class TestFourStates(ExecutorCase):
    def test_right(self) -> None:
        out = self.run_case(SUM_SOLVE, stdin="1 2 3\n", expected="6\n")
        self.assertEqual(out["type"], "right", out)
        self.assertEqual(out["code"], "6\n")

    def test_wrong(self) -> None:
        out = self.run_case(SUM_SOLVE, stdin="1 2 3\n", expected="999\n")
        self.assertEqual(out["type"], "wrong", out)
        self.assertEqual(out["result"], "6\n")

    def test_runtime_error_on_division_by_zero(self) -> None:
        out = self.run_case(DIV_ZERO, stdin="")
        self.assertEqual(out["type"], "runtime_error", out)
        self.assertIn("ZeroDivisionError", str(out.get("code") or ""))

    def test_runtime_error_on_timeout(self) -> None:
        out = self.run_case(INFINITE_LOOP, stdin="", timeout_sec=1.0)
        self.assertEqual(out["type"], "runtime_error", out)
        self.assertIn("time", out, "超时必须带 time 字段（供上层映射 TLE 口径）")


class TestOjStyleKnownGap(ExecutorCase):
    @unittest.expectedFailure
    def test_oj_input_is_right(self) -> None:
        """P1 目标：OJ 式 input() 应判 right。当前环境变量传 stdin，取不到数据。"""
        out = self.run_case(OJ_INPUT, stdin="6\n", expected="6")
        self.assertEqual(out["type"], "right", out)

    @unittest.expectedFailure
    def test_oj_sys_stdin_read_is_right(self) -> None:
        """P1 目标：OJ 式 sys.stdin.read() 应判 right。同上。"""
        out = self.run_case(OJ_STDIN_READ, stdin="1 2 3\n", expected="6")
        self.assertEqual(out["type"], "right", out)

    def test_current_behavior_recorded(self) -> None:
        """把当前事实写死：OJ 式代码现在拿不到 via stdin 的数据（非 right）。"""
        out = self.run_case(OJ_INPUT, stdin="6\n", expected="6")
        self.assertNotEqual(out["type"], "right", f"执行器似乎已支持 OJ 式 stdin，请更新本文件：{out}")


if __name__ == "__main__":
    unittest.main()
