"""统一执行接口：`run_case(lang, src, stdin, timeout_ms)`。

契约（设计 §6，P1 冻结）::

    run_case("python", src, stdin, 2000)
    -> {"verdict": "ac", "stdout": "...", "stderr": "", "rc": 0, "elapsed_ms": 12.3, ...}

verdict 取值：`ac` / `re` / `tle` / `ce` / `system_error`。
（`wa` 由比较器结合期望输出判定，本层只判"跑没跑成"，不判"对不对"。）

**编译次数（S7，审查严重项）**：早期实现每条用例都调一次 `run_case`，C/C++ 因此**每用例
重新编译**（实测 cpp 20 条 12.3s）。现在拆成三段，调用方可以"编译一次、跑多条"：

    program = prepare_program("cpp", code)      # 只编译一次
    for case in cases:
        run_prepared(program, case.input, 2000) # 只跑二进制
    release_program(program)

`run_case` 仍然是一次性语义（内部 prepare→run→release），行为与旧版完全一致。

实现要点：
- 每次运行独立临时工作目录，跑完删除（防被测代码写脏仓库）。
- C/C++ 编译与运行分开计时：`elapsed_ms` 只含运行，`compile_ms` 单独回填。
- 编译失败 `verdict=ce`，`stderr` 为编译器原文（UI 直接回填给学生）。
- 超时由 `proc.run_process` 杀进程树，本层只翻译成 `tle`。
- 本函数是纯函数式副作用（文件 + 进程），不读 DB、不调 LLM（R2）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .compilers import compile_cpp, probe_compiler
from .proc import ProcResult, RunnerEnvError, python_executable, run_process

LANG_PYTHON = "python"
LANG_CPP = "cpp"
LANG_C = "c"
SUPPORTED_LANGS: tuple[str, ...] = (LANG_PYTHON, LANG_CPP, LANG_C)

VERDICT_AC = "ac"
VERDICT_RE = "re"
VERDICT_TLE = "tle"
VERDICT_CE = "ce"
VERDICT_SYSTEM_ERROR = "system_error"

_SOURCE_SUFFIX = {LANG_PYTHON: ".py", LANG_CPP: ".cpp", LANG_C: ".c"}
_EXE_SUFFIX = ".exe"

# 运行期线程栈：Windows 默认 1MB，深递归题在 6.3.0 上会栈溢出。
# 不改线程栈（需要链接期 -Wl,--stack），交给题目自身约束，文档记为已知局限。


@dataclass
class CaseResult:
    """单用例结果。`to_dict()` 是回传主理人/入库的稳定结构。"""

    verdict: str
    stdout: str = ""
    stderr: str = ""
    rc: int = 0
    elapsed_ms: float = 0.0
    compile_ms: float = 0.0
    compile_output: str = ""
    lang: str = ""
    timed_out: bool = False
    output_truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "rc": int(self.rc),
            "elapsed_ms": float(self.elapsed_ms),
            "compile_ms": float(self.compile_ms),
            "compile_output": self.compile_output,
            "lang": self.lang,
            "timed_out": bool(self.timed_out),
            "output_truncated": bool(self.output_truncated),
            "notes": list(self.notes),
        }


@dataclass
class PreparedProgram:
    """一次编译/落盘的结果，可被多条用例复用（S7 的核心）。"""

    lang: str
    workdir: Path
    script_path: Optional[Path] = None
    exe_path: Optional[Path] = None
    ok: bool = True
    compile_ms: float = 0.0
    compile_output: str = ""
    error: str = ""
    temporary: bool = True


def _translate(proc: ProcResult, *, lang: str, compile_ms: float = 0.0,
               compile_output: str = "") -> CaseResult:
    """把进程结果翻译成 verdict。判定优先级：超时 > 非零退出 > ac。"""
    if proc.timed_out:
        verdict = VERDICT_TLE
    elif proc.rc != 0:
        verdict = VERDICT_RE
    elif proc.output_truncated:
        # 输出被截断说明行为已不可信，按基础设施故障记录，不冒充 ac
        verdict = VERDICT_SYSTEM_ERROR
    else:
        verdict = VERDICT_AC
    notes: list[str] = []
    if proc.output_truncated:
        notes.append("output_truncated")
    return CaseResult(
        verdict=verdict,
        stdout=proc.stdout,
        stderr=proc.stderr,
        rc=proc.rc,
        elapsed_ms=proc.elapsed_ms,
        compile_ms=compile_ms,
        compile_output=compile_output,
        lang=lang,
        timed_out=proc.timed_out,
        output_truncated=proc.output_truncated,
        notes=notes,
    )


def _build_workdir(workdir: Optional[Path]) -> tuple[Path, bool]:
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, False
    return Path(tempfile.mkdtemp(prefix="autocode_run_")), True


def prepare_program(
    lang: str,
    src: str,
    *,
    compile_timeout_ms: int = 20_000,
    workdir: Optional[Path] = None,
) -> PreparedProgram:
    """落盘源码并（C/C++）编译一次。返回的 `PreparedProgram` 可反复 `run_prepared`。"""
    if lang not in SUPPORTED_LANGS:
        raise RunnerEnvError(f"不支持的语言 {lang!r}，可选 {SUPPORTED_LANGS}")
    if not isinstance(src, str) or not src.strip():
        raise RunnerEnvError("源码为空")

    dir_path, is_temp = _build_workdir(workdir)
    program = PreparedProgram(lang=lang, workdir=dir_path, temporary=is_temp)
    if lang == LANG_PYTHON:
        script = dir_path / "main.py"
        script.write_text(src, encoding="utf-8")
        program.script_path = script
        return program

    family = "g++" if lang == LANG_CPP else "gcc"
    if probe_compiler(family) is None:
        raise RunnerEnvError(f"环境缺少编译器 {family}，请在设置中安装或指定 AUTOCODE_CPP_COMPILER")
    src_path = dir_path / f"main{_SOURCE_SUFFIX[lang]}"
    src_path.write_text(src, encoding="utf-8")
    exe_path = dir_path / f"main_{lang}{_EXE_SUFFIX if os.name == 'nt' else ''}"
    started = time.perf_counter()
    compiled = compile_cpp(src_path, exe_path, family=family, timeout_ms=compile_timeout_ms)
    program.compile_ms = round((time.perf_counter() - started) * 1000.0, 3)
    program.compile_output = compiled.stderr or compiled.stdout
    if compiled.rc != 0 or compiled.timed_out or not exe_path.exists():
        program.ok = False
        program.error = "compiler_timeout" if compiled.timed_out else "compile_error"
        return program
    program.exe_path = exe_path
    return program


def run_prepared(
    program: PreparedProgram,
    stdin: str,
    timeout_ms: int,
    *,
    args: Sequence[str] = (),
) -> Dict[str, Any]:
    """在已准备好的程序上跑一条用例（不再编译）。返回结构同 `run_case`。"""
    if not program.ok:
        return CaseResult(
            verdict=VERDICT_CE,
            stderr=program.compile_output,
            rc=0,
            elapsed_ms=0.0,
            compile_ms=program.compile_ms,
            compile_output=program.compile_output,
            lang=program.lang,
            notes=[program.error or "compile_error"],
        ).to_dict()

    if program.lang == LANG_PYTHON:
        if program.script_path is None:
            raise RunnerEnvError("PreparedProgram 缺少 script_path")
        argv = [python_executable(), "-X", "utf8", str(program.script_path), *args]
    else:
        if program.exe_path is None:
            raise RunnerEnvError("PreparedProgram 缺少 exe_path")
        argv = [str(program.exe_path), *args]

    proc = run_process(argv, stdin_text=stdin, cwd=program.workdir, timeout_ms=timeout_ms)
    result = _translate(
        proc,
        lang=program.lang,
        compile_ms=program.compile_ms,
        compile_output=program.compile_output.strip(),
    )
    return result.to_dict()


def release_program(program: Optional[PreparedProgram]) -> None:
    """删除临时工作目录（调用方传了 workdir 的不删，保持旧语义）。"""
    if program is None or not program.temporary:
        return
    shutil.rmtree(program.workdir, ignore_errors=True)


def run_case(
    lang: str,
    src: str,
    stdin: str,
    timeout_ms: int,
    *,
    args: Sequence[str] = (),
    workdir: Optional[Path] = None,
    compile_timeout_ms: int = 20_000,
    keep_workdir: bool = False,
) -> Dict[str, Any]:
    """跑一次用例，返回 dict（见模块文档）。语义 = prepare → run → release。

    Args:
        lang: `python` / `cpp` / `c`。
        src : 完整源码。
        stdin: 喂给程序的标准输入（空串表示无输入）。
        timeout_ms: wall-clock 超时（毫秒），到点杀进程树。
        args: 追加的命令行参数（generator 子进程化时用 seed/profile）。
        workdir: 指定工作目录（不给则临时目录，跑完删除）。
        compile_timeout_ms: 编译超时（C/C++）。
        keep_workdir: 保留工作目录（排查用）。

    Raises:
        RunnerEnvError: 语言不支持或编译器缺失（环境问题，不是被测代码问题）。
    """
    program = prepare_program(
        lang, src, compile_timeout_ms=compile_timeout_ms, workdir=workdir
    )
    try:
        return run_prepared(program, stdin, timeout_ms, args=args)
    finally:
        if not keep_workdir:
            release_program(program)
