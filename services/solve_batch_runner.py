from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal


def _build_solve_wrapper_script(code: str) -> str:
    code = code or ""
    return "\n".join(
        [
            code.rstrip(),
            "",
            "import json",
            "import os",
            "import sys",
            "import traceback",
            "",
            "def __runner__():",
            "    stdin = sys.stdin.read()",
            "    try:",
            "        fn = globals().get('solve')",
            "        if not callable(fn):",
            "            raise RuntimeError('solve_not_found')",
            "        case_json = os.environ.get('CASE_DATA_JSON') or ''",
            "        data = {}",
            "        if case_json.strip():",
            "            try:",
            "                data = json.loads(case_json)",
            "            except Exception:",
            "                data = {}",
            "        if not isinstance(data, dict):",
            "            data = {}",
            "        data.setdefault('stdout', '')",
            "        data.setdefault('meta', {})",
            "        data.setdefault('vars', {})",
            "        data['stdin'] = stdin",
            "        res = fn({}, data)",
            "        if isinstance(res, dict):",
            "            out = res.get('stdout', '')",
            "        elif res is None:",
            "            out = ''",
            "        else:",
            "            out = str(res)",
            "        sys.stdout.write('' if out is None else str(out))",
            "    except Exception:",
            "        traceback.print_exc()",
            "        return 1",
            "    return 0",
            "",
            "raise SystemExit(__runner__())",
            "",
        ]
    )


def _write_temp_script(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", delete=False) as f:
        f.write(text or "")
        return f.name


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


class SolveBatchRunner(QObject):
    case_started = Signal(int)
    case_finished = Signal(int, dict)
    all_finished = Signal()
    runner_error = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._python_exe = sys.executable
        self._timeout_sec = 3.0

        self._script_path = ""
        self._proc: Optional[QProcess] = None
        self._queue: List[Dict[str, Any]] = []
        self._current_index: Optional[int] = None
        self._case_start_ts = 0.0
        self._timeout_timer: Optional[QTimer] = None
        self._timeout_fired = False

    def is_running(self) -> bool:
        return bool(self._running)

    def start(
        self,
        *,
        solve_code: str,
        cases: List[Dict[str, Any]],
        timeout_sec: float = 3.0,
        python_exe: Optional[str] = None,
        cached_results: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> None:
        if self._running:
            return

        if not (solve_code or "").strip():
            self.runner_error.emit("empty_solve_code")
            return

        self.stop()

        self._python_exe = (python_exe or sys.executable) or sys.executable
        self._timeout_sec = float(timeout_sec or 3.0)
        self._queue = []
        for i, c in enumerate(cases or []):
            case_dict = dict(c or {})
            item: Dict[str, Any] = {"index": i, "case": case_dict, "stdin": str(case_dict.get("stdin") or "")}
            if cached_results and i in cached_results:
                item["cached_payload"] = dict(cached_results.get(i) or {})
            self._queue.append(item)

        if not self._queue:
            self.all_finished.emit()
            return

        self._script_path = _write_temp_script(_build_solve_wrapper_script(solve_code))
        self._running = True
        self.running_changed.emit(True)
        self._run_next()

    def stop(self) -> None:
        was_running = self._running
        self._running = False
        self._queue = []
        self._current_index = None
        self._timeout_fired = False

        self._stop_timer()

        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.deleteLater()
            except Exception:
                pass
            self._proc = None

        _safe_unlink(self._script_path)
        self._script_path = ""

        if was_running:
            self.running_changed.emit(False)

    def _stop_timer(self) -> None:
        t = self._timeout_timer
        self._timeout_timer = None
        if t is None:
            return
        try:
            t.stop()
        except Exception:
            pass
        try:
            t.deleteLater()
        except Exception:
            pass

    def _run_next(self) -> None:
        if not self._running:
            return
        if not self._queue:
            self._running = False
            self.running_changed.emit(False)
            self.all_finished.emit()
            _safe_unlink(self._script_path)
            self._script_path = ""
            return

        item = self._queue.pop(0)
        row_idx = int(item.get("index"))
        stdin_text = str(item.get("stdin") or "")
        case_dict = dict(item.get("case") or {})
        cached_payload = item.get("cached_payload")

        if cached_payload is not None:
            self._current_index = row_idx
            self._timeout_fired = bool(dict(cached_payload or {}).get("timed_out", False))
            self._case_start_ts = time.perf_counter()
            self.case_started.emit(row_idx)

            payload = dict(cached_payload or {})
            self._emit_finished(
                row_idx,
                exit_code=int(payload.get("exit_code", 0)),
                exit_status=payload.get("exit_status", QProcess.NormalExit),
                stdout=str(payload.get("stdout") or ""),
                stderr=str(payload.get("stderr") or ""),
                timed_out=bool(payload.get("timed_out", False)),
            )
            self._current_index = None
            self._run_next()
            return

        self._current_index = row_idx
        self._timeout_fired = False
        self._case_start_ts = time.perf_counter()
        self.case_started.emit(row_idx)

        proc = QProcess(self)
        self._proc = proc
        proc.setProgram(self._python_exe)
        proc.setArguments([self._script_path])
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        try:
            import json

            env = QProcessEnvironment.systemEnvironment()
            env.insert("CASE_DATA_JSON", json.dumps(case_dict, ensure_ascii=True))
            proc.setProcessEnvironment(env)
        except Exception:
            pass

        self._stop_timer()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_timeout)
        self._timeout_timer = timer
        timer.start(max(1, int(self._timeout_sec * 1000)))

        proc.start()
        if not proc.waitForStarted(800):
            self._stop_timer()
            self._emit_finished(
                row_idx,
                exit_code=-1,
                exit_status=int(QProcess.NormalExit),
                stdout="",
                stderr="process_start_failed",
                timed_out=False,
            )
            self._current_index = None
            self._run_next()
            return

        if stdin_text:
            proc.write(stdin_text.encode("utf-8"))
        proc.closeWriteChannel()

    def _on_timeout(self) -> None:
        if self._current_index is None:
            return
        self._timeout_fired = True
        proc = self._proc
        if proc is not None and proc.state() != QProcess.NotRunning:
            try:
                proc.kill()
            except Exception:
                pass

    def _on_error(self, err: QProcess.ProcessError) -> None:
        _ = err

    def _read_output(self, proc: Optional[QProcess]) -> tuple[str, str]:
        if proc is None:
            return "", ""
        try:
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        except Exception:
            out = ""
        try:
            err = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace")
        except Exception:
            err = ""
        return out, err

    def _emit_finished(
        self,
        row_idx: int,
        *,
        exit_code: int,
        exit_status: Any,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> None:
        ms = int((time.perf_counter() - self._case_start_ts) * 1000)
        try:
            exit_status_int = int(getattr(exit_status, "value"))
        except Exception:
            try:
                exit_status_int = int(exit_status)
            except Exception:
                exit_status_int = -1
        payload = {
            "exit_code": int(exit_code),
            "exit_status": exit_status_int,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "elapsed_ms": ms,
            "timed_out": bool(timed_out),
        }
        self.case_finished.emit(int(row_idx), payload)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._stop_timer()
        row_idx = self._current_index
        if row_idx is None:
            return

        stdout, stderr = self._read_output(self._proc)
        timed_out = bool(self._timeout_fired)
        self._emit_finished(
            row_idx,
            exit_code=int(exit_code),
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

        self._current_index = None
        self._run_next()
