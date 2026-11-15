"""执行引擎：统一 `run_case` + 编译器探测。

对外只暴露稳定契约，内部三块：
- `proc.py`      子进程底座（env 白名单 / stdin 管道 / 超时杀树 / 输出上限）
- `compilers.py` g++/gcc 探测与编译（本机实测 GCC 6.3.0 → `-std=c++14`）
- `core.py`      `run_case` 语言分派与 verdict 翻译

约束（`tools/check_layering.py` 强制）：本包禁 import `autocode.{llm,agent,app,ui}`。
"""

from __future__ import annotations

from .compilers import CPP_STD, CompilerInfo, compile_cpp, compiler_report, probe_compiler
from .core import (
    LANG_C,
    LANG_CPP,
    LANG_PYTHON,
    SUPPORTED_LANGS,
    VERDICT_AC,
    VERDICT_CE,
    VERDICT_RE,
    VERDICT_SYSTEM_ERROR,
    VERDICT_TLE,
    CaseResult,
    PreparedProgram,
    prepare_program,
    release_program,
    run_case,
    run_prepared,
)
from .proc import (
    DEFAULT_MAX_OUTPUT_CHARS,
    ENV_WHITELIST,
    ProcResult,
    RunnerEnvError,
    build_env,
    python_executable,
    run_process,
)

__all__ = [
    "CPP_STD",
    "CompilerInfo",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "ENV_WHITELIST",
    "LANG_C",
    "LANG_CPP",
    "LANG_PYTHON",
    "SUPPORTED_LANGS",
    "VERDICT_AC",
    "VERDICT_CE",
    "VERDICT_RE",
    "VERDICT_SYSTEM_ERROR",
    "VERDICT_TLE",
    "CaseResult",
    "PreparedProgram",
    "ProcResult",
    "RunnerEnvError",
    "build_env",
    "compile_cpp",
    "compiler_report",
    "prepare_program",
    "probe_compiler",
    "python_executable",
    "release_program",
    "run_case",
    "run_prepared",
    "run_process",
]
