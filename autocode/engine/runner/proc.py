"""子进程统一底座：环境白名单、stdin 管道、超时杀进程树、输出上限。

为什么单独一层（设计 §6）：
- `run_case` 与 generator 子进程化都要用同一套启停逻辑，"一处实现两处复用"
  才不会有"generator 忘了杀进程树"这类分叉。
- 禁环境变量传数据：被测代码可能把 env 当输入，一旦允许，样例与真实评测
  会走两条路径，判题结果不可信。数据只走 stdin 管道。

Windows 细节：
- `CREATE_NO_WINDOW`：连跑 30 用例不允许弹黑框。
- 超时只 kill 父进程不够（被测代码可能自己起子进程），用 `taskkill /T` 杀树。
- 输出上限：死循环 print 会把内存打满，超限截断并置 `output_truncated`。

本模块不含任何判定逻辑，只负责"把程序跑起来并收回三件套"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

IS_WINDOWS = os.name == "nt"

# Windows: 不弹控制台窗口
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# env 白名单：只保留程序启动必需的，杜绝把 DEEPSEEK_API_KEY 之类泄进被测进程。
ENV_WHITELIST: tuple[str, ...] = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "LANG",
    "LC_ALL",
    "HOME",
    "USERPROFILE",
)

# 单次运行输出上限（字符数，非字节）；超过即截断并标记。
DEFAULT_MAX_OUTPUT_CHARS = 64_000_000
# 16MB 会误杀合法数据：文件夹合并（n=5e5、m≤3n）实测约 20MB，
# 被截断后判 system_error，整题失败。16MB 只影响单次进程读取内存，仍能挡住死循环打印。


class RunnerEnvError(RuntimeError):
    """启动前置条件不满足（例如缺编译器）。"""


@dataclass(frozen=True)
class ProcResult:
    """一次子进程运行的结果。`elapsed_ms` 为 wall-clock，含进程启动开销。"""

    rc: int
    stdout: str
    stderr: str
    elapsed_ms: float
    timed_out: bool
    output_truncated: bool = False


def build_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """构造白名单环境变量；`extra` 只允许追加白名单外的显式键（测试用）。"""
    env: Dict[str, str] = {}
    for key in ENV_WHITELIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # 固定编码，避免子进程按 cp936 输出导致判定随机
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONHASHSEED"] = "0"
    # 老 Windows 缺 SYSTEMROOT 时部分 CRT 会崩，兜底
    env.setdefault("SYSTEMROOT", os.environ.get("SYSTEMROOT", r"C:\Windows"))
    if extra:
        env.update(dict(extra))
    return env


def _popen_extra() -> Dict[str, object]:
    """Windows/Linux 通用的启动参数差异收敛在此。"""
    kwargs: Dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _kill_tree(pid: int) -> None:
    """杀进程树；失败不抛（进程可能已退出）。"""
    if IS_WINDOWS:
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        argv = [str(taskkill), "/F", "/T", "/PID", str(pid)]
        if not taskkill.exists():
            argv = ["taskkill", "/F", "/T", "/PID", str(pid)]
        try:
            subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _decode(raw: Optional[bytes], limit: int) -> tuple[str, bool]:
    if not raw:
        return "", False
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), truncated


def _secret_dirs() -> list[str]:
    """沙箱里必须遮掉的目录：库文件、管理员凭证、设置文件的父目录。

    背景（2026-11-22 实测）：`--ro-bind / /` 把整个根目录只读挂进沙箱，而跑学生代码的
    用户就是服务用户，于是 `print(open('/opt/autocode/data/slim2.db','rb').read())`
    能把整库（含全部密码哈希与题库）从 stdout 带出来。这些目录对被测程序毫无用处，
    直接 tmpfs 盖成空目录，路径存在但内容为空。
    """
    dirs: list[str] = []
    for key in ("AUTOCODE_DB", "AUTOCODE_SLIM_CREDENTIALS", "AUTOCODE_SETTINGS_FILE"):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        parent = str(Path(raw).expanduser().resolve().parent)
        if parent != "/" and parent not in dirs:
            dirs.append(parent)
    return dirs


def _sandbox_command(argv: Sequence[str], cwd: Optional[Path]) -> list[str]:
    """按 `AUTOCODE_SANDBOX=bwrap` 包一层沙箱；默认不包，完整版行为不变。

    只做四件事：根文件系统只读、遮掉数据目录、断网、工作目录可写。
    刻意不做 per-run 用户隔离（那需要一次性容器），本周上线够用。
    """
    mode = str(os.environ.get("AUTOCODE_SANDBOX") or "").strip().lower()
    if mode != "bwrap":
        return list(argv)
    workdir = str(cwd) if cwd else os.getcwd()
    masks: list[str] = []
    for path in _secret_dirs():
        masks += ["--tmpfs", path]
    # 不传 `--proc /proc`：在 systemd 加固单元（ProtectSystem=strict）里挂载新 procfs
    # 会报 "Can't mount proc on /newroot/proc: Operation not permitted"，
    # 而 `--unshare-pid` 本身可用。保留 pid 命名空间（学生代码无法向宿主机进程发信号），
    # 放弃重挂 proc（沙箱内 /proc 仍是宿主视角，属已知的可接受折中）。
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        *masks,
        "--tmpfs", "/tmp",
        "--unshare-net",
        "--unshare-pid",
        "--die-with-parent",
        "--bind", workdir, workdir,
        "--chdir", workdir,
        "--",
        *argv,
    ]


def run_process(
    argv: Sequence[str],
    *,
    stdin_text: str = "",
    cwd: Optional[Path] = None,
    timeout_ms: int = 2000,
    env: Optional[Mapping[str, str]] = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> ProcResult:
    """跑一条命令并收集输出；超时杀进程树并置 `timed_out=True`。"""
    if not argv:
        raise RunnerEnvError("argv 不能为空")
    argv = _sandbox_command(argv, cwd)
    started = time.perf_counter()
    kwargs = _popen_extra()
    try:
        proc = subprocess.Popen(  # noqa: S603 - 被测程序，路径均来自本机探测
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=build_env(env),
            **kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        raise RunnerEnvError(f"无法启动子进程 {argv[0]!r}: {exc}") from exc

    timed_out = False
    stdin_bytes = (stdin_text or "").encode("utf-8")
    try:
        out_b, err_b = proc.communicate(input=stdin_bytes, timeout=max(0.01, timeout_ms / 1000.0))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_b, err_b = b"", b""
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    stdout, out_cut = _decode(out_b, max_output_chars)
    stderr, err_cut = _decode(err_b, max_output_chars)
    rc = int(proc.returncode if proc.returncode is not None else -1)
    return ProcResult(
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=round(elapsed_ms, 3),
        timed_out=timed_out,
        output_truncated=out_cut or err_cut,
    )


def python_executable() -> str:
    """返回当前解释器绝对路径（venv 内，保证与主进程同版本）。"""
    return sys.executable or "python"
