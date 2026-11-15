"""autoCode 分层 / 禁名 / 行数门禁（R2 + R4，AST 实现，非文本匹配）。

为什么用 AST 而不是 grep：
- 文本匹配会被注释、字符串、docstring 误伤（例如本文档里就写着被禁的名字），
  也会被 `from ..llm import x` 这类相对导入绕过。
- AST 能精确拿到 import 目标与表达式中的 Name/Attribute，误报与漏报都更少。

检查项：
1. `engine/` 禁 import `autocode.{llm, agent, app, ui}`（R2：engine 零 LLM）。
2. `engine/` 内禁出现 `importlib` / `__import__` / `exec` / `eval` 名字
   （扫 Name / Attribute；`Call` 的 func 就是前两者之一，天然覆盖）。
3. `utils/` 禁 import 任何 autocode 业务包（agent/engine/llm/store/app/ui）。
4. `ui/` 只允许经 `app/` 访问业务：禁 import `autocode.{agent, engine, llm, store}`。
5. `llm/` 禁 import `autocode.{agent, app, ui}`（LLM 客户端不反向依赖编排/内核/界面）。
6. 文件 ≤800 行、函数 ≤150 行（R4）。

import 判定覆盖的写法（漏一种就等于开了后门）：
- `import autocode.llm.http`            -> 直接命中 `autocode.llm`
- `from autocode.llm import http`       -> 直接命中 `autocode.llm`
- `from ..llm import http`（相对导入）  -> 解析为绝对名后命中
- `from autocode import llm`            -> 逐 alias 拼接成 `autocode.llm` 后命中
- `from ... import llm`（空 module）    -> 按解析后的包路径逐 alias 拼接后命中
- `import autocode` + 后续属性访问      -> 对 `Import` 做**反向前缀匹配**：
  `autocode.llm` 以 `autocode.` 开头，故 `import autocode` 也算越界（无法静态排除）

行数口径（R4）：
- 文件行数按物理行计（`len(text.splitlines())`）。
- 函数行数 = `end_lineno - def 行号 + 1`。**装饰器不计入**（Python 3.8+ 的
  `FunctionDef.lineno` 指向 `def` 行，装饰器在 `decorator_list` 里各带 lineno）。
- **嵌套函数分别计一次，且其代码同时计入父函数**：父子都超限就各报一条，
  这样"把 200 行塞进一个内部函数来糊弄父函数"不会漏检。

allowlist（`tools/layering_allowlist.txt`）：
- 行格式 `相对路径:行号:规则  # 必填理由`；缺理由视为格式错误（直接退出码 1）。
- identity = (路径, 行号, 规则)：同一文件里**新增**一条同类违规不会被历史条目兜住。
- 命中项在输出里逐条打印（含理由），绝不静默放过；声明了但不再触发的列为"失效条目"，
  且**失效条目同样使退出码为 1**（防止清单腐烂成噪声）。

已知覆盖不到（best-effort，配合 code review）：
- 字符串拼接后 `getattr(builtins, "ex" + "ec")`、`__loader__` 反射等元编程绕过；
- 运行期 `sys.modules` 注入。
本门禁的目标是拦住"顺手 import / 顺手 eval"的日常退化，不是对抗恶意代码。

CLI：
    python -m autocode.tools.check_layering [--root DIR] [--pkg NAME]
                                            [--allowlist FILE] [--no-allowlist]
退出码：0 = 无未登记违规且无失效条目；1 = 存在未登记违规 / 失效条目 / 格式错误。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 150

PKG_NAME = "autocode"

# 规则 1：engine 零 LLM（R2）
ENGINE_FORBIDDEN_PREFIXES: Tuple[str, ...] = (
    "autocode.llm",
    "autocode.agent",
    "autocode.app",
    "autocode.ui",
)
# 规则 2：engine 内禁名（防动态导入绕过 R2）
ENGINE_BANNED_NAMES: Tuple[str, ...] = ("importlib", "__import__", "exec", "eval")
# 规则 3：utils 纯工具，禁依赖任何业务包
UTILS_FORBIDDEN_PREFIXES: Tuple[str, ...] = (
    "autocode.agent",
    "autocode.engine",
    "autocode.llm",
    "autocode.store",
    "autocode.app",
    "autocode.ui",
)
# 规则 4：ui 只能经 app
UI_FORBIDDEN_PREFIXES: Tuple[str, ...] = (
    "autocode.agent",
    "autocode.engine",
    "autocode.llm",
    "autocode.store",
)
# 规则 5：llm 是下游叶子，不得反向依赖编排/内核/界面
LLM_FORBIDDEN_PREFIXES: Tuple[str, ...] = (
    "autocode.agent",
    "autocode.app",
    "autocode.ui",
)

RULE_ENGINE_IMPORT = "layer-engine-import"
RULE_ENGINE_BANNED_NAME = "engine-banned-name"
RULE_UTILS_IMPORT = "layer-utils-import"
RULE_UI_IMPORT = "layer-ui-import"
RULE_LLM_IMPORT = "layer-llm-import"
RULE_FILE_SIZE = "size-file"
RULE_FUNCTION_SIZE = "size-function"
RULE_PARSE_ERROR = "parse-error"
RULE_READ_ERROR = "read-error"


@dataclass(frozen=True)
class Violation:
    """一条门禁违规。path 为相对扫描根目录的 POSIX 风格路径。"""

    path: str
    lineno: int
    rule: str
    message: str

    def identity(self) -> Tuple[str, int, str]:
        return (self.path, int(self.lineno), self.rule)

    def render(self) -> str:
        return f"{self.path}:{self.lineno}: [{self.rule}] {self.message}"


@dataclass(frozen=True)
class AllowlistEntry:
    """allowlist 声明：`path:lineno:rule  # 理由`。"""

    path: str
    lineno: int
    rule: str
    reason: str
    source_lineno: int

    def identity(self) -> Tuple[str, int, str]:
        return (self.path, int(self.lineno), self.rule)

    def render(self) -> str:
        return f"{self.path}:{self.lineno}:{self.rule}  ({self.reason})"


# ── 文件 / 模块名校验 ────────────────────────────────────────────────────────


def iter_python_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _module_parts(root: Path, path: Path, pkg_name: str) -> List[str]:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [pkg_name] + parts if parts else [pkg_name]


def _package_parts(root: Path, path: Path, pkg_name: str) -> List[str]:
    module_parts = _module_parts(root, path, pkg_name)
    if path.name == "__init__.py":
        return module_parts
    return module_parts[:-1]


def _resolve_from_import(package_parts: Sequence[str], node: ast.ImportFrom) -> str:
    """把 `from ..x import y` 解析成绝对点分模块名（只到 module 层，不含 alias）。"""
    parts = list(package_parts)
    level = int(getattr(node, "level", 0) or 0)
    if level:
        drop = level - 1
        if drop >= len(parts):
            parts = []
        else:
            parts = parts[: len(parts) - drop]
    else:
        # 绝对导入：package_parts 的首段（即包名本身）不加前缀
        parts = []
    if node.module:
        parts.extend(str(node.module).split("."))
    return ".".join(part for part in parts if part)


def _iter_imports(tree: ast.AST, package_parts: Sequence[str]) -> Iterable[Tuple[int, str, bool]]:
    """产出 (行号, 模块名, 是否普通 import)。

    - 普通 `import a.b`：产出 ("a.b", True)，后续还会做反向前缀匹配。
    - `from a import b`：产出 ("a", False) 与 ("a.b", False)，后者用于抓
      `from autocode import llm` / `from ... import llm` 这类"模块名藏着包名"的写法。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield (node.lineno, str(alias.name), True)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_import(package_parts, node)
            if not base:
                continue
            yield (node.lineno, base, False)
            for alias in node.names:
                name = str(alias.name or "").strip()
                if not name or name == "*":
                    continue
                yield (node.lineno, f"{base}.{name}", False)


def _match_prefix(module: str, prefixes: Sequence[str]) -> Optional[str]:
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return None


def _match_reverse_prefix(module: str, prefixes: Sequence[str]) -> Optional[str]:
    """`import autocode` 这类粗粒度导入：禁区内任何子模块都可能被属性访问碰到。"""
    stem = module + "."
    for prefix in prefixes:
        if prefix.startswith(stem):
            return prefix
    return None


# ── 单文件检查 ──────────────────────────────────────────────────────────────


def _check_imports(
    *,
    rel_path: str,
    tree: ast.AST,
    package_parts: Sequence[str],
    top_dir: str,
) -> List[Violation]:
    if top_dir == "engine":
        prefixes, rule = ENGINE_FORBIDDEN_PREFIXES, RULE_ENGINE_IMPORT
        scope = "engine/ 禁止 import autocode.{llm,agent,app,ui}（R2 engine 零 LLM）"
    elif top_dir == "utils":
        prefixes, rule = UTILS_FORBIDDEN_PREFIXES, RULE_UTILS_IMPORT
        scope = "utils/ 禁止 import autocode 业务包（纯工具层）"
    elif top_dir == "ui":
        prefixes, rule = UI_FORBIDDEN_PREFIXES, RULE_UI_IMPORT
        scope = "ui/ 禁止 import autocode.{agent,engine,llm,store}（只能经 app）"
    elif top_dir == "llm":
        prefixes, rule = LLM_FORBIDDEN_PREFIXES, RULE_LLM_IMPORT
        scope = "llm/ 禁止 import autocode.{agent,app,ui}（LLM 客户端不反向依赖）"
    else:
        return []

    # 同一行可能产出多个候选（from a import b 会同时得到 a 与 a.b），按行聚合，
    # 保证"一条 import 语句最多报一条"，避免同一问题刷屏掩盖真问题。
    per_line: Dict[int, Tuple[str, str]] = {}
    for lineno, module, is_plain_import in _iter_imports(tree, package_parts):
        hit = _match_prefix(module, prefixes)
        if hit is None and is_plain_import:
            hit = _match_reverse_prefix(module, prefixes)
        if hit is not None:
            per_line.setdefault(int(lineno), (module, hit))

    return [
        Violation(
            path=rel_path,
            lineno=lineno,
            rule=rule,
            message=f"{scope}；实际导入 {module}（命中禁区 {hit}）",
        )
        for lineno, (module, hit) in sorted(per_line.items())
    ]


def _check_engine_banned_names(*, rel_path: str, tree: ast.AST) -> List[Violation]:
    violations: List[Violation] = []
    banned = set(ENGINE_BANNED_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in banned:
            violations.append(
                Violation(
                    path=rel_path,
                    lineno=int(getattr(node, "lineno", 0) or 0),
                    rule=RULE_ENGINE_BANNED_NAME,
                    message=f"engine/ 内禁止出现名字 {node.id!r}（防动态导入绕过 R2）",
                )
            )
        elif isinstance(node, ast.Attribute) and node.attr in banned:
            violations.append(
                Violation(
                    path=rel_path,
                    lineno=int(getattr(node, "lineno", 0) or 0),
                    rule=RULE_ENGINE_BANNED_NAME,
                    message=f"engine/ 内禁止出现属性访问 .{node.attr!r}（防动态导入绕过 R2）",
                )
            )
    return violations


def _check_sizes(*, rel_path: str, tree: ast.AST, total_lines: int) -> List[Violation]:
    violations: List[Violation] = []
    if total_lines > MAX_FILE_LINES:
        violations.append(
            Violation(
                path=rel_path,
                lineno=1,
                rule=RULE_FILE_SIZE,
                message=f"文件 {total_lines} 行 > 上限 {MAX_FILE_LINES}（R4）",
            )
        )
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end_lineno = int(getattr(node, "end_lineno", 0) or 0)
        if end_lineno <= 0:
            continue
        # 装饰器不计入（node.lineno 指向 def 行）；嵌套函数分别计一次，且计入父函数长度。
        length = end_lineno - int(node.lineno) + 1
        if length > MAX_FUNCTION_LINES:
            violations.append(
                Violation(
                    path=rel_path,
                    lineno=int(node.lineno),
                    rule=RULE_FUNCTION_SIZE,
                    message=f"函数 {node.name} 长 {length} 行 > 上限 {MAX_FUNCTION_LINES}（R4）",
                )
            )
    return violations


def check_file(root: Path, path: Path, *, pkg_name: str = PKG_NAME) -> List[Violation]:
    """检查单个文件；语法错误按违规上报（门禁不能因为解析失败就放过）。"""
    rel_path = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [
            Violation(
                path=rel_path,
                lineno=int(exc.lineno or 0),
                rule=RULE_PARSE_ERROR,
                message=f"无法解析为 Python AST：{exc.msg}",
            )
        ]
    except OSError as exc:
        return [Violation(path=rel_path, lineno=0, rule=RULE_READ_ERROR, message=f"读取失败：{exc}")]

    rel_parts = Path(rel_path).parts
    top_dir = rel_parts[0] if len(rel_parts) > 1 else ""
    package_parts = _package_parts(root, path, pkg_name)

    violations: List[Violation] = []
    violations.extend(
        _check_imports(rel_path=rel_path, tree=tree, package_parts=package_parts, top_dir=top_dir)
    )
    if top_dir == "engine":
        violations.extend(_check_engine_banned_names(rel_path=rel_path, tree=tree))
    violations.extend(
        _check_sizes(rel_path=rel_path, tree=tree, total_lines=len(text.splitlines()))
    )
    return violations


def collect_violations(root: Path, *, pkg_name: str = PKG_NAME) -> List[Violation]:
    """扫描整棵包树，返回全部违规（未做 allowlist 过滤）。"""
    violations: List[Violation] = []
    for path in iter_python_files(root):
        violations.extend(check_file(root, path, pkg_name=pkg_name))
    return violations


# ── allowlist ───────────────────────────────────────────────────────────────


def load_allowlist(path: Path) -> List[AllowlistEntry]:
    """读取 allowlist：`相对路径:行号:规则  # 理由`；`#` 开头为注释。

    缺理由、缺行号、字段为空一律视为格式错误并抛 ValueError（不允许"沉默的放过"）。
    """
    if not path.exists():
        return []
    entries: List[AllowlistEntry] = []
    for source_lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, sep, reason = line.partition("#")
        body = body.strip()
        reason = reason.strip()
        if not sep or not reason:
            raise ValueError(
                f"{path.name}:{source_lineno}: 必须写明理由，格式 '相对路径:行号:规则  # 理由'，实际 {line!r}"
            )
        parts = body.split(":")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError(
                f"{path.name}:{source_lineno}: 允许清单格式应为 '相对路径:行号:规则'，实际 {body!r}"
            )
        file_part, lineno_part, rule_part = (part.strip() for part in parts)
        try:
            lineno = int(lineno_part)
        except ValueError as exc:
            raise ValueError(
                f"{path.name}:{source_lineno}: 行号必须是整数，实际 {lineno_part!r}"
            ) from exc
        entries.append(
            AllowlistEntry(
                path=file_part.replace("\\", "/"),
                lineno=lineno,
                rule=rule_part,
                reason=reason,
                source_lineno=source_lineno,
            )
        )
    return entries


def split_violations(
    violations: Sequence[Violation],
    allowlist: Sequence[AllowlistEntry],
) -> Tuple[List[Violation], List[Tuple[Violation, AllowlistEntry]], List[AllowlistEntry]]:
    """拆成 (未登记违规, 已登记且命中的违规, 声明了但未命中的条目)。"""
    index: Dict[Tuple[str, int, str], AllowlistEntry] = {entry.identity(): entry for entry in allowlist}
    unlisted: List[Violation] = []
    allowed: List[Tuple[Violation, AllowlistEntry]] = []
    hit: set[Tuple[str, int, str]] = set()
    for violation in violations:
        entry = index.get(violation.identity())
        if entry is None:
            unlisted.append(violation)
            continue
        allowed.append((violation, entry))
        hit.add(entry.identity())
    stale = [entry for entry in allowlist if entry.identity() not in hit]
    return unlisted, allowed, stale


# ── CLI ─────────────────────────────────────────────────────────────────────


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_allowlist() -> Path:
    return Path(__file__).resolve().parent / "layering_allowlist.txt"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autocode.tools.check_layering",
        description="autoCode 分层 / 禁名 / 行数门禁（R2 + R4）",
    )
    parser.add_argument("--root", default=str(_default_root()), help="扫描根目录（默认 autocode 包目录）")
    parser.add_argument("--pkg", default=PKG_NAME, help="包名（用于把相对导入解析为绝对模块名）")
    parser.add_argument("--allowlist", default=str(_default_allowlist()), help="允许清单文件路径")
    parser.add_argument("--no-allowlist", action="store_true", help="忽略允许清单，全部违规都报错")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"错误：扫描根目录不存在：{root}", file=sys.stderr)
        return 1

    files = iter_python_files(root)
    violations = collect_violations(root, pkg_name=str(args.pkg))

    allowlist_path = Path(args.allowlist)
    try:
        allowlist = [] if args.no_allowlist else load_allowlist(allowlist_path)
    except ValueError as exc:
        print(f"错误：允许清单解析失败：{exc}", file=sys.stderr)
        return 1

    unlisted, allowed, stale = split_violations(violations, allowlist)

    print("=== autoCode 分层门禁（R2 engine 零 LLM + R4 行数）===")
    print(f"扫描根目录: {root}")
    print(f"Python 文件: {len(files)}")
    print(f"允许清单: {allowlist_path}（{len(allowlist)} 条）")
    print()

    print(f"[allowlist 已登记放过] {len(allowed)} 条")
    if allowed:
        for violation, entry in allowed:
            print(f"  - {violation.render()}")
            print(f"    理由: {entry.reason}")
    else:
        print("  （无）")
    print()

    if stale:
        print(f"[allowlist 失效条目] {len(stale)} 条（已不触发，必须清理，否则退出码为 1）")
        for entry in stale:
            print(f"  - 第{entry.source_lineno}行 {entry.render()}")
        print()

    print(f"[未登记违规] {len(unlisted)} 条")
    if unlisted:
        for violation in unlisted:
            print(f"  - {violation.render()}")
    else:
        print("  （无）")
    print()

    if unlisted or stale:
        print(
            f"结果: FAIL（未登记违规 {len(unlisted)} 条，失效 allowlist 条目 {len(stale)} 条）"
        )
        return 1
    print("结果: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
