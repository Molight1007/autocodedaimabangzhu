"""check_layering 门禁自测。

守三件事：
1. 真实仓库（autocode/）跑门禁必须 PASS（退出码 0、未登记违规 0 条）。
2. 临时目录里伪造的每一条违规都必须被抓出来（不能是"永远绿"的橡皮门禁）。
3. allowlist 只能"降级为可见记录"，不能静默放过：条目命中要在输出里逐条打印，失效条目要提示。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autocode.tools import check_layering as gate
from autocode.tools.check_layering import (
    RULE_ENGINE_BANNED_NAME,
    RULE_ENGINE_IMPORT,
    RULE_FILE_SIZE,
    RULE_FUNCTION_SIZE,
    RULE_LLM_IMPORT,
    RULE_UI_IMPORT,
    RULE_UTILS_IMPORT,
    check_file,
    collect_violations,
    load_allowlist,
    main,
    split_violations,
)

PKG_ROOT = PROJECT_ROOT / "autocode"
ALLOWLIST = PKG_ROOT / "tools" / "layering_allowlist.txt"


def _write(root: Path, rel_path: str, text: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestRealRepository(unittest.TestCase):
    def test_repository_has_no_unlisted_violation(self) -> None:
        violations = collect_violations(PKG_ROOT)
        unlisted, allowed, stale = split_violations(violations, load_allowlist(ALLOWLIST))
        self.assertEqual(
            [item.render() for item in unlisted],
            [],
            "autocode 存在未登记门禁违规",
        )
        self.assertEqual(
            [entry.render() for entry in stale],
            [],
            "allowlist 有条目已失效，请清理",
        )
        # allowed 里只允许放"确实登记且命中"的条目，数量必须与声明一致
        self.assertEqual(len(allowed), len(load_allowlist(ALLOWLIST)))

    def test_cli_returns_zero_on_repository(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(PKG_ROOT), "--allowlist", str(ALLOWLIST)])
        output = buffer.getvalue()
        self.assertEqual(code, 0, output)
        self.assertIn("结果: PASS", output)

    def test_scans_reasonable_number_of_files(self) -> None:
        files = gate.iter_python_files(PKG_ROOT)
        self.assertGreaterEqual(len(files), 15)
        self.assertTrue(all(path.suffix == ".py" for path in files))


class TestFabricatedViolations(unittest.TestCase):
    """临时目录伪造违规，逐条验证能抓出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root, "__init__.py", "")
        _write(self.root, "engine/__init__.py", "")
        _write(self.root, "engine/rel_import.py", "from ..llm import http\n")
        _write(self.root, "engine/abs_import.py", "import autocode.agent.trace\n")
        _write(self.root, "engine/banned_names.py", "import importlib\n\n\ndef f(x):\n    importlib.reload(x)\n    return eval(x)\n")
        _write(self.root, "utils/leaky.py", "from ..store import db\n")
        _write(self.root, "ui/leaky.py", "from ..engine import runner\n")
        _write(self.root, "engine/huge_file.py", "".join("x = 1\n" for _ in range(820)))
        _write(
            self.root,
            "engine/long_function.py",
            "def too_long():\n" + "".join("    x = 1\n" for _ in range(160)),
        )
        # S4：三种曾漏检的写法
        _write(self.root, "engine/from_pkg.py", "from autocode import llm\n")
        # `from ... import llm` 需要包深足够：engine/verify/x.py 的 level=3 才解析到 autocode
        _write(self.root, "engine/verify/__init__.py", "")
        _write(self.root, "engine/verify/from_dots.py", "from ... import llm\n")
        _write(self.root, "engine/bare_pkg.py", "import autocode\n\n\ndef touch():\n    return autocode.llm\n")
        # S4 反例：与 autocode 无关的 import 不得误报
        _write(self.root, "engine/unrelated.py", "from abc import ABC\n")
        self.violations = collect_violations(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render(self) -> str:
        return "\n".join(item.render() for item in self.violations)

    def test_engine_relative_import_detected(self) -> None:
        found = [
            item
            for item in self.violations
            if item.rule == RULE_ENGINE_IMPORT and item.path == "engine/rel_import.py"
        ]
        self.assertEqual(len(found), 1, self._render())
        self.assertIn("autocode.llm", found[0].message)

    def test_engine_absolute_import_detected(self) -> None:
        found = [
            item
            for item in self.violations
            if item.rule == RULE_ENGINE_IMPORT and item.path == "engine/abs_import.py"
        ]
        self.assertEqual(len(found), 1, self._render())
        self.assertIn("autocode.agent", found[0].message)

    def test_engine_from_package_import_detected(self) -> None:
        """S4-1：`from autocode import llm` 必须命中（alias 拼成 autocode.llm）。"""
        found = [
            item
            for item in self.violations
            if item.rule == RULE_ENGINE_IMPORT and item.path == "engine/from_pkg.py"
        ]
        self.assertEqual(len(found), 1, self._render())
        self.assertIn("autocode.llm", found[0].message)

    def test_engine_from_dots_import_detected(self) -> None:
        """S4-2：`from ... import llm`（空 module 相对导入）必须命中。"""
        found = [
            item
            for item in self.violations
            if item.rule == RULE_ENGINE_IMPORT and item.path == "engine/verify/from_dots.py"
        ]
        self.assertEqual(len(found), 1, self._render())
        self.assertIn("autocode.llm", found[0].message)

    def test_engine_bare_package_import_detected(self) -> None:
        """S4-3：`import autocode` 后用属性访问，必须按反向前缀命中。"""
        found = [
            item
            for item in self.violations
            if item.rule == RULE_ENGINE_IMPORT and item.path == "engine/bare_pkg.py"
        ]
        self.assertEqual(len(found), 1, self._render())
        self.assertIn("autocode.llm", found[0].message)

    def test_unrelated_import_not_flagged(self) -> None:
        """S4 反例：`from abc import x` 不得误报。"""
        found = [item for item in self.violations if item.path == "engine/unrelated.py"]
        self.assertEqual(found, [], self._render())

    def test_engine_banned_names_detected(self) -> None:
        found = [item for item in self.violations if item.rule == RULE_ENGINE_BANNED_NAME]
        messages = " ".join(item.message for item in found)
        self.assertIn("importlib", messages)
        self.assertIn("'eval'", messages)
        self.assertEqual({item.path for item in found}, {"engine/banned_names.py"})

    def test_utils_and_ui_import_rules_detected(self) -> None:
        rules = {item.rule for item in self.violations}
        self.assertIn(RULE_UTILS_IMPORT, rules, self._render())
        self.assertIn(RULE_UI_IMPORT, rules, self._render())

    def test_file_and_function_size_detected(self) -> None:
        rules = {item.rule for item in self.violations}
        self.assertIn(RULE_FILE_SIZE, rules, self._render())
        self.assertIn(RULE_FUNCTION_SIZE, rules, self._render())
        long_function = [item for item in self.violations if item.rule == RULE_FUNCTION_SIZE]
        self.assertIn("too_long", long_function[0].message)

    def test_clean_file_has_no_violation(self) -> None:
        clean = _write(self.root, "engine/clean.py", "import ast\n\n\ndef f(x):\n    return ast.parse(x)\n")
        self.assertEqual(check_file(self.root, clean), [])

    def test_syntax_error_is_reported_not_skipped(self) -> None:
        broken = _write(self.root, "engine/broken.py", "def f(:\n")
        violations = check_file(self.root, broken)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "parse-error")

    def test_cli_returns_one_without_allowlist(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(self.root), "--no-allowlist"])
        output = buffer.getvalue()
        self.assertEqual(code, 1, output)
        self.assertIn("结果: FAIL", output)
        self.assertIn(RULE_ENGINE_IMPORT, output)


class TestAllowlistMechanism(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root, "__init__.py", "")
        _write(self.root, "engine/__init__.py", "")
        _write(self.root, "engine/legacy.py", "def f(x):\n    return exec(x)\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _allowlist(self, body: str) -> Path:
        path = self.root / "allowlist.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def test_allowlisted_violation_is_downgraded_but_listed(self) -> None:
        allowlist_path = self._allowlist(
            "# 历史遗留\nengine/legacy.py:2:engine-banned-name  # 存量代码，P1 移除\n"
        )
        entries = load_allowlist(allowlist_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].lineno, 2)
        unlisted, allowed, stale = split_violations(collect_violations(self.root), entries)
        self.assertEqual(unlisted, [])
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0][1].reason, "存量代码，P1 移除")
        self.assertEqual(stale, [])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(self.root), "--allowlist", str(allowlist_path)])
        output = buffer.getvalue()
        self.assertEqual(code, 0, output)
        self.assertIn("已登记放过", output)
        self.assertIn("存量代码，P1 移除", output)

    def test_allowlisted_entry_is_line_precise(self) -> None:
        """M3：行号参与 identity —— 同文件新增同类违规不会被历史条目兜住。"""
        allowlist_path = self._allowlist(
            "engine/legacy.py:2:engine-banned-name  # 只豁免第 2 行\n"
        )
        _write(self.root, "engine/legacy.py", "def f(x):\n    return exec(x)\n\n\ndef g(x):\n    return exec(x)\n")
        unlisted, allowed, stale = split_violations(collect_violations(self.root), load_allowlist(allowlist_path))
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(unlisted), 1)
        self.assertEqual(unlisted[0].lineno, 6)
        self.assertEqual(stale, [])

    def test_stale_allowlist_entry_blocks_exit_zero(self) -> None:
        """M3：失效条目必须让退出码为 1（防清单腐烂成噪声）。"""
        allowlist_path = self._allowlist("engine/gone.py:1:size-file  # 文件已删除\n")
        unlisted, allowed, stale = split_violations(collect_violations(self.root), load_allowlist(allowlist_path))
        self.assertEqual(len(unlisted), 1)
        self.assertEqual(allowed, [])
        self.assertEqual(len(stale), 1)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(self.root), "--allowlist", str(allowlist_path)])
        output = buffer.getvalue()
        self.assertEqual(code, 1, output)
        self.assertIn("失效条目", output)

    def test_stale_only_exits_one(self) -> None:
        """只存在失效条目、无未登记违规时，退出码同样是 1。"""
        allowlist_path = self._allowlist(
            "engine/legacy.py:2:engine-banned-name  # 正确豁免\n"
            "engine/gone.py:1:size-file  # 失效条目\n"
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(self.root), "--allowlist", str(allowlist_path)])
        self.assertEqual(code, 1, buffer.getvalue())

    def test_missing_reason_is_format_error(self) -> None:
        allowlist_path = self._allowlist("engine/legacy.py:2:engine-banned-name\n")
        with self.assertRaises(ValueError) as ctx:
            load_allowlist(allowlist_path)
        self.assertIn("理由", str(ctx.exception))

    def test_two_part_legacy_format_rejected(self) -> None:
        allowlist_path = self._allowlist("engine/legacy.py:engine-banned-name  # 旧格式缺行号\n")
        with self.assertRaises(ValueError):
            load_allowlist(allowlist_path)

    def test_non_integer_lineno_rejected(self) -> None:
        allowlist_path = self._allowlist("engine/legacy.py:abc:engine-banned-name  # 行号非法\n")
        with self.assertRaises(ValueError):
            load_allowlist(allowlist_path)

    def test_cli_reports_allowlist_parse_error(self) -> None:
        allowlist_path = self._allowlist("engine/legacy.py 无冒号无理由\n")
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = main(["--root", str(self.root), "--allowlist", str(allowlist_path)])
        self.assertEqual(code, 1)
        self.assertIn("允许清单解析失败", buffer.getvalue())


class TestLlmLayerRule(unittest.TestCase):
    """M1：llm 不得反向 import autocode.{agent,app,ui}。"""

    def test_llm_importing_agent_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "__init__.py", "")
            _write(root, "llm/__init__.py", "")
            _write(root, "llm/bad.py", "from ..agent.contracts import protocol\n")
            _write(root, "llm/ok.py", "from ..engine.verify import verdict\n")
            violations = collect_violations(root)
            by_path = {item.path: item for item in violations}
            self.assertIn("llm/bad.py", by_path)
            self.assertEqual(by_path["llm/bad.py"].rule, RULE_LLM_IMPORT)
            self.assertNotIn("llm/ok.py", by_path)


if __name__ == "__main__":
    unittest.main()
