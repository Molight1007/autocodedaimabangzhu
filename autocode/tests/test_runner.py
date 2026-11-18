"""执行引擎测试：三语言 run_case、超时杀进程、env 白名单、CE、generator 参数透传。

真实起进程（不 mock），因为这一层的价值就在于"真的能跑"。
C/C++ 相关断言在缺编译器的机器上自动 skip（设计 §6 降级口径）。
"""

from __future__ import annotations

import unittest

from autocode.engine.runner import (
    LANG_CPP,
    LANG_PYTHON,
    VERDICT_AC,
    VERDICT_CE,
    VERDICT_RE,
    VERDICT_TLE,
    build_env,
    compiler_report,
    probe_compiler,
    run_case,
)

HAS_CPP = probe_compiler("g++") is not None


class TestPythonRun(unittest.TestCase):
    def test_input_builtin(self) -> None:
        src = "n = int(input())\na = list(map(int, input().split()))\nprint(sum(a))\n"
        result = run_case(LANG_PYTHON, src, "3\n1 2 3\n", 5000)
        self.assertEqual(result["verdict"], VERDICT_AC)
        self.assertEqual(result["stdout"].strip(), "6")
        self.assertEqual(result["rc"], 0)

    def test_stdin_read_and_print_without_newline(self) -> None:
        src = "import sys\nd = sys.stdin.read().split()\nprint(int(d[0]) * 2, end='')\n"
        result = run_case(LANG_PYTHON, src, "21\n", 5000)
        self.assertEqual(result["verdict"], VERDICT_AC)
        self.assertEqual(result["stdout"].strip(), "42")

    def test_runtime_error_reports_stderr(self) -> None:
        result = run_case(LANG_PYTHON, "raise SystemExit(3)\n", "", 5000)
        self.assertEqual(result["verdict"], VERDICT_RE)
        self.assertEqual(result["rc"], 3)

    def test_timeout_kills_and_marks_tle(self) -> None:
        result = run_case(LANG_PYTHON, "while True:\n    pass\n", "", 300)
        self.assertEqual(result["verdict"], VERDICT_TLE)
        self.assertTrue(result["timed_out"])

    def test_args_passthrough_for_generator(self) -> None:
        src = "import sys\nprint(sys.argv[1], sys.argv[2])\n"
        result = run_case(LANG_PYTHON, src, "", 5000, args=("42", "max"))
        self.assertEqual(result["stdout"].strip(), "42 max")

    def test_empty_source_rejected(self) -> None:
        with self.assertRaises(Exception):
            run_case(LANG_PYTHON, "   ", "", 1000)

    def test_unknown_lang_rejected(self) -> None:
        with self.assertRaises(Exception):
            run_case("rust", "fn main(){}", "", 1000)


class TestEnvWhitelist(unittest.TestCase):
    def test_secret_not_inherited(self) -> None:
        """被测进程不得看到父进程的密钥类环境变量（禁环境变量传数据/泄密）。"""
        src = (
            "import os\n"
            "print('DEEPSEEK_API_KEY' in os.environ)\n"
            "print('PATH' in os.environ)\n"
        )
        result = run_case(LANG_PYTHON, src, "", 5000)
        lines = result["stdout"].split()
        self.assertEqual(lines[0], "False")
        self.assertEqual(lines[1], "True")

    def test_whitelist_contains_required_keys(self) -> None:
        env = build_env()
        for key in ("PATH", "SYSTEMROOT", "TEMP", "LANG", "PYTHONIOENCODING"):
            self.assertIn(key, env)


@unittest.skipUnless(HAS_CPP, "环境无 g++，跳过 C/C++ 用例")
class TestCppRun(unittest.TestCase):
    def test_compile_and_run(self) -> None:
        src = '#include <cstdio>\nint main(){int n;scanf("%d",&n);printf("%d\\n",n*3);return 0;}\n'
        result = run_case(LANG_CPP, src, "7\n", 10_000)
        self.assertEqual(result["verdict"], VERDICT_AC)
        self.assertEqual(result["stdout"].strip(), "21")
        self.assertGreater(result["compile_ms"], 0.0)

    def test_compile_error_original_text(self) -> None:
        result = run_case(LANG_CPP, "int main(){ this is not cpp }\n", "", 10_000)
        self.assertEqual(result["verdict"], VERDICT_CE)
        self.assertIn("error", result["compile_output"].lower())

    def test_cpp14_standard_compiles(self) -> None:
        """本机 GCC 6.3.0 不认 c++17，只能用 c++14：auto 返回类型推导 + 初始化列表。"""
        src = (
            "#include <cstdio>\n#include <vector>\n"
            "int main(){ std::vector<int> v{1,2,3}; int s=0; for(auto x : v) s+=x;"
            "printf(\"%d\\n\", s); return 0; }\n"
        )
        result = run_case(LANG_CPP, src, "", 10_000)
        self.assertEqual(result["verdict"], VERDICT_AC, result["compile_output"])
        self.assertEqual(result["stdout"].strip(), "6")


class TestCompilerReport(unittest.TestCase):
    def test_report_shape(self) -> None:
        report = compiler_report()
        self.assertIn("python", report)
        self.assertIn("cpp", report)
        self.assertIn("c", report)
        self.assertIsNotNone(report["python"])


if __name__ == "__main__":
    unittest.main()


class TestPrepareOnce(unittest.TestCase):
    """S7：编译一次、多用例复用（旧实现每用例重编译，cpp 20 条 12.3s）。"""

    def test_python_prepare_run_release(self) -> None:
        from autocode.engine.runner import prepare_program, release_program, run_prepared

        program = prepare_program(LANG_PYTHON, "import sys\nprint(int(sys.stdin.read().strip())*2)\n")
        self.assertTrue(program.ok)
        self.assertTrue(program.script_path.exists())
        first = run_prepared(program, "3", 5000)
        second = run_prepared(program, "10", 5000)
        self.assertEqual(first["stdout"].strip(), "6")
        self.assertEqual(second["stdout"].strip(), "20")
        workdir = program.workdir
        release_program(program)
        self.assertFalse(workdir.exists())

    @unittest.skipUnless(HAS_CPP, "环境无 g++，跳过")
    def test_cpp_compiled_once_for_many_cases(self) -> None:
        """S7 回归：5 条用例整批只编译 1 次。

        断言口径：**数编译次数**，不量墙钟。旧实现每用例重编译（cpp 20 条约 12.3s），
        用"绝对耗时 <2s"来判会让这条测试随机器负载抖动（实测 1.0~2.9s），
        而它的真实意图只是"别逐用例重编译"——数调用次数既精确又稳定。
        """
        import time
        from unittest.mock import patch

        from autocode.app import judge
        from autocode.engine.runner import core as runner_core

        code = (
            '#include <cstdio>\nint main(){int n;if(scanf("%d",&n)!=1)return 0;'
            'printf("%d\\n",n*2);return 0;}\n'
        )
        cases = [{"id": f"c{i}", "input": f"{i}\n", "expected": f"{i * 2}\n"} for i in range(5)]
        compile_calls: list = []
        real_compile_cpp = runner_core.compile_cpp

        def counting_compile(*args, **kwargs):
            compile_calls.append(args[0] if args else kwargs.get("src_path"))
            return real_compile_cpp(*args, **kwargs)

        started = time.perf_counter()
        with patch.object(runner_core, "compile_cpp", counting_compile):
            batch = judge.run_solution("cpp", code, cases, time_limit_ms=2000)
        elapsed = time.perf_counter() - started

        self.assertTrue(batch.all_accepted, batch.verdict_counts())
        self.assertEqual(
            len(compile_calls), 1,
            f"5 条用例应只编译 1 次，实际 {len(compile_calls)} 次（逐用例重编译）；耗时 {elapsed:.2f}s",
        )

    @unittest.skipUnless(HAS_CPP, "环境无 g++，跳过")
    def test_compile_error_reported_once(self) -> None:
        from autocode.app import judge

        batch = judge.run_solution("cpp", "int main(){ this is not cpp }", [{"id": "c0", "input": "1"}])
        self.assertTrue(batch.compile_error)
        self.assertEqual(batch.runs, [])
