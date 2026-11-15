"""内核移植完整性测试。

守三件事：
1. autocode 下每个模块都能真实导入（移植断引用会在此处红，而不是等到跑 UI 才炸）。
2. autocode 内不残留对旧 `services.*` 包的依赖（AST 级判定，比文本 grep 精确）。
3. 关键符号的落位符合设计 §2 映射表（不靠人肉记忆）。
"""

from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PKG_ROOT = PROJECT_ROOT / "autocode"
LEGACY_PACKAGE = "services"


def _iter_python_files() -> list[Path]:
    return sorted(path for path in PKG_ROOT.rglob("*.py") if path.is_file())


def _module_name(path: Path) -> str:
    rel = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class TestKernelImports(unittest.TestCase):
    def test_package_root_on_path(self) -> None:
        self.assertTrue((PKG_ROOT / "__init__.py").is_file())

    def test_every_module_imports(self) -> None:
        failures: list[str] = []
        modules = [_module_name(path) for path in _iter_python_files()]
        self.assertGreater(len(modules), 15, "autocode 包文件数异常，可能路径算错")

        for name in modules:
            with self.subTest(module=name):
                try:
                    importlib.import_module(name)
                except Exception as exc:  # noqa: BLE001 - 这里就是要收集所有导入异常
                    failures.append(f"{name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "以下模块导入失败:\n" + "\n".join(failures))

    def test_no_legacy_package_imports_in_source(self) -> None:
        """AST 扫描 import 目标，确认没有指向旧包的名字。"""
        offenders: list[str] = []
        for path in _iter_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(PKG_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [str(node.module or "")]
                else:
                    continue
                for module in names:
                    if module == LEGACY_PACKAGE or module.startswith(LEGACY_PACKAGE + "."):
                        offenders.append(f"{rel}:{node.lineno}: {module}")
        self.assertEqual(offenders, [], "仍有指向旧包的导入:\n" + "\n".join(offenders))


class TestDesignPlacement(unittest.TestCase):
    """设计 §2 映射表的关键落位断言。"""

    def test_guard_moved_to_engine_sandbox(self) -> None:
        module = importlib.import_module("autocode.engine.sandbox.agent_guard")
        self.assertTrue(callable(module.check_code_safety))
        self.assertFalse((PKG_ROOT / "agent" / "runtime" / "guard.py").exists())

    def test_py_validator_moved_to_engine_sandbox(self) -> None:
        module = importlib.import_module("autocode.engine.sandbox.py_validator")
        self.assertTrue(callable(module.validate_python_source))

    def test_verdict_lives_in_engine_verify(self) -> None:
        module = importlib.import_module("autocode.engine.verify.verdict")
        self.assertTrue(hasattr(module, "VerifyVerdict"))

    def test_analyzer_lives_in_llm_not_engine(self) -> None:
        module = importlib.import_module("autocode.llm.analyzer")
        self.assertTrue(callable(module.analyze_problem))
        self.assertFalse((PKG_ROOT / "engine" / "analyzer.py").exists(), "含 LLM 提示词的 analyzer 不得进 engine")

    def test_trace_lives_at_agent_root(self) -> None:
        module = importlib.import_module("autocode.agent.trace")
        self.assertTrue(callable(module.start_agent_trace_run))
        expected = PROJECT_ROOT / "logs"
        self.assertEqual(module._resolve_logs_dir(), expected)

    def test_deprecated_modules_are_not_ported(self) -> None:
        self.assertFalse((PKG_ROOT / "agent" / "verifier").exists(), "verifier 子包应已拆解")
        for leftover in ("cross_runner.py", "data_pool.py"):
            matches = list(PKG_ROOT.rglob(leftover))
            self.assertEqual(matches, [], f"{leftover} 按设计弃用，不应存在")

    def test_facade_exposes_solve_entry(self) -> None:
        module = importlib.import_module("autocode.agent.facade")
        self.assertTrue(callable(module.solve_with_agent))
        self.assertFalse(hasattr(module, "verify_with_cross_agent"), "跨验证入口依赖弃用模块，P0 已移除")

    def test_stream_is_importable(self) -> None:
        for name in (
            "autocode.agent.controller.autonomous_loop",
            "autocode.agent.controller.dispatcher",
            "autocode.agent.controller.messages",
            "autocode.agent.controller.context_tools",
            "autocode.agent.controller.api",
            "autocode.agent.runtime.executor",
            "autocode.agent.session.state",
            "autocode.agent.session.artifact_session",
        ):
            with self.subTest(module=name):
                self.assertTrue(importlib.import_module(name))


class TestNoLegacyTextRemains(unittest.TestCase):
    def test_no_legacy_import_statement_text(self) -> None:
        """保守兜底：即使 AST 通过，也不允许出现旧包导入语句的文本痕迹。"""
        forbidden = ("from " + LEGACY_PACKAGE, "import " + LEGACY_PACKAGE)
        hits: list[str] = []
        for path in _iter_python_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if any(token in stripped for token in forbidden):
                    hits.append(f"{path.relative_to(PKG_ROOT).as_posix()}:{lineno}: {stripped}")
        self.assertEqual(hits, [], "残留旧包导入语句:\n" + "\n".join(hits))


class TestRepositoryHygiene(unittest.TestCase):
    """仓库卫生：测试基线必须真的能入库（B1 回归守卫）。

    `.gitignore` 里旧工程的 `tests/` 若无前导斜杠，git 会匹配任意层级，
    把 `autocode/tests/` 整体忽略——本地全绿但文件根本进不了仓库。
    这里用 `git check-ignore -q` 把这个坑钉死（-q 的退出码才是真实忽略判定；
    -v 在存在否定模式时会把否定条目也打印出来并返回 0，容易误判）。
    """

    def test_tests_directory_is_not_gitignored(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("环境没有 git，跳过仓库卫生检查")
        completed = subprocess.run(
            [git, "check-ignore", "-q", "autocode/tests/test_loop_behavior.py"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if completed.returncode == 128:
            self.skipTest("当前目录不是 git 仓库，跳过")
        self.assertEqual(
            completed.returncode,
            1,
            "autocode/tests/ 被 .gitignore 忽略了：检查 `.gitignore` 里的 `tests/` 是否缺前导斜杠",
        )


if __name__ == "__main__":
    unittest.main()
