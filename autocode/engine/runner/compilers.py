"""编译器探测与编译（C/C++）。

口径说明（实测环境：MinGW.org GCC 6.3.0）：
- 该版本**不支持 `-std=c++17`**（会退化成 c++1z 告警或直接报错），故 C++ 统一用
  `-std=c++14`；C 用 `-std=c11`（6.3.0 已支持）。
- 探测顺序：环境变量 `AUTOCODE_CPP_COMPILER` → PATH → 常见安装位（含本项目实测的
  `D:/mingw/bin`）。探测结果缓存，避免每用例 fork 一次 `--version`。
- 缺编译器不抛穿透：`probe_compiler` 返回 None，上层按"三语言降级为两语言"处理
  （设计 §6 编译器探测降级），题目标 `lang_available`。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .proc import ProcResult, RunnerEnvError, python_executable, run_process

CPP_STD = "c++14"
C_STD = "c11"
COMPILE_TIMEOUT_MS = 20_000

# Windows 常见安装位（含本机实测路径），仅作兜底；不存在则忽略。
_COMMON_SEARCH_DIRS: tuple[str, ...] = (
    r"D:\mingw\bin",
    r"C:\mingw64\bin",
    r"C:\mingw32\bin",
    r"C:\MinGW\bin",
    r"C:\msys64\mingw64\bin",
    r"C:\TDM-GCC-64\bin",
    r"C:\Program Files\mingw-w64\mingw64\bin",
    r"C:\Program Files (x86)\mingw-w64\mingw64\bin",
)


@dataclass(frozen=True)
class CompilerInfo:
    """一个可用编译器的路径与版本串。"""

    family: str  # "gcc" / "g++"
    path: str
    version: str

    def to_dict(self) -> Dict[str, str]:
        return {"family": self.family, "path": self.path, "version": self.version}


_PROBE_CACHE: Dict[str, Optional[CompilerInfo]] = {}


def _candidate_paths(names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    override = os.environ.get("AUTOCODE_CPP_COMPILER", "").strip()
    if override:
        found.append(override)
    for name in names:
        hit = shutil.which(name)
        if hit:
            found.append(hit)
    for name in names:
        for base in _COMMON_SEARCH_DIRS:
            candidate = Path(base) / name
            if candidate.exists():
                found.append(str(candidate))
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        key = os.path.normcase(os.path.abspath(item))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _read_version(path: str) -> str:
    try:
        result = run_process([path, "--version"], timeout_ms=10_000)
    except RunnerEnvError:
        return ""
    first = (result.stdout or result.stderr or "").splitlines()
    return first[0].strip() if first else ""


def probe_compiler(family: str) -> Optional[CompilerInfo]:
    """探测 g++/gcc，返回 None 表示不可用（降级信号，不抛异常）。"""
    if family in _PROBE_CACHE:
        return _PROBE_CACHE[family]
    names = ("g++.exe", "g++") if family == "g++" else ("gcc.exe", "gcc")
    info: Optional[CompilerInfo] = None
    for path in _candidate_paths(names):
        version = _read_version(path)
        if version:
            info = CompilerInfo(family=family, path=path, version=version)
            break
    _PROBE_CACHE[family] = info
    return info


def compile_cpp(
    src_path: Path,
    exe_path: Path,
    *,
    family: str = "g++",
    extra_flags: tuple[str, ...] = (),
    timeout_ms: int = COMPILE_TIMEOUT_MS,
) -> ProcResult:
    """编译源码；返回 `ProcResult`（rc==0 即成功，stderr 为编译器原文，回填 CE 信息）。

    注意：`-std` 只对 g++/gcc 生效；`-static` 在 MinGW 上会让 exe 变大但更可控，
    这里不加（编译时长优先）。`-O2` 与评测机口径一致。
    """
    info = probe_compiler(family)
    if info is None:
        raise RunnerEnvError(f"未找到可用编译器 {family}（PATH 与常见安装位均未命中）")
    std = CPP_STD if family == "g++" else C_STD
    argv = [
        info.path,
        "-O2",
        f"-std={std}",
        "-o",
        str(exe_path),
        str(src_path),
        *extra_flags,
    ]
    src_path.parent.mkdir(parents=True, exist_ok=True)
    # cwd 必须给到源码目录：沙箱用 `--tmpfs /tmp` 覆盖临时目录，只绑定 cwd，
    # 不传 cwd 时绑定的是服务目录，编译器就看不到 /tmp/autocode_run_* 里的源文件
    # （报 cc1plus: fatal error: .../main.cpp: No such file or directory）。
    return run_process(
        argv, timeout_ms=timeout_ms, max_output_chars=200_000, cwd=src_path.parent
    )


def compiler_report() -> Dict[str, Optional[Dict[str, str]]]:
    """三语言可用性快照，供题目 `lang_available` 与 UI 降级提示使用。"""
    cpp = probe_compiler("g++")
    cc = probe_compiler("gcc")
    return {
        "python": {"family": "python", "path": python_executable(), "version": "venv"},
        "cpp": cpp.to_dict() if cpp else None,
        "c": cc.to_dict() if cc else None,
    }
