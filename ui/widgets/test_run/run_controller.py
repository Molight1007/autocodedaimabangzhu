from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class TestRunController:
    def __init__(
        self,
        *,
        cases: List[Dict[str, Any]],
        cache_get: Callable[[str], Optional[Dict[str, Any]]],
        cache_put: Callable[[str, Dict[str, Any]], None],
        make_cache_key: Callable[..., str],
    ):
        self._cases: List[Dict[str, Any]] = list(cases or [])
        self._cache_get = cache_get
        self._cache_put = cache_put
        self._make_cache_key = make_cache_key
        self.runs: List[Dict[str, Any]] = []
        for i, c in enumerate(self._cases):
            self.runs.append(
                {
                    "index": i,
                    "case": dict(c or {}),
                    "status": "Pending",
                    "elapsed_ms": None,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": False,
                }
            )
        self.max_ready_index = 0 if self._cases else -1

    def get_run(self, idx: int) -> Optional[Dict[str, Any]]:
        if 0 <= int(idx) < len(self.runs):
            return self.runs[int(idx)]
        return None

    def count_done(self) -> int:
        done = 0
        for r in self.runs:
            if str(r.get("status")) in {"OK", "RE", "Timeout"}:
                done += 1
        return done

    def reset_examples_state(self) -> None:
        for r in self.runs:
            r["status"] = "Pending"
            r["elapsed_ms"] = None
            r["stdout"] = ""
            r["stderr"] = ""
            r["timed_out"] = False
        self.max_ready_index = 0 if self.runs else -1

    def collect_cached_results(self, *, code: str) -> Dict[int, Dict[str, Any]]:
        cached_results: Dict[int, Dict[str, Any]] = {}
        for i, c in enumerate(self._cases):
            key = self._make_cache_key(code=code, case=dict(c or {}))
            payload = self._cache_get(key)
            if payload:
                cached_results[int(i)] = payload
        return cached_results

    def on_batch_case_started(self, row_idx: int) -> bool:
        r = self.get_run(int(row_idx))
        if not r:
            return False
        r["status"] = "Running"
        self.max_ready_index = max(self.max_ready_index, int(row_idx))
        return True

    def on_batch_case_finished(self, row_idx: int, payload: Dict[str, Any], *, code: str) -> bool:
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
        elapsed_ms = payload.get("elapsed_ms")
        timed_out = bool(payload.get("timed_out"))
        exit_code = int(payload.get("exit_code", -1))

        status = "Timeout" if timed_out else ("OK" if exit_code == 0 else "RE")

        r = self.get_run(int(row_idx))
        if not r:
            return False
        r["status"] = status
        r["elapsed_ms"] = elapsed_ms
        r["stdout"] = stdout
        r["stderr"] = stderr
        r["timed_out"] = timed_out
        self.max_ready_index = max(self.max_ready_index, int(row_idx))

        try:
            case = dict(self._cases[int(row_idx)] or {})
        except Exception:
            case = {}
        key = self._make_cache_key(code=code, case=case)
        self._cache_put(key, dict(payload or {}))
        return True

    def on_batch_all_finished(self) -> None:
        if self.runs:
            self.max_ready_index = len(self.runs) - 1

    def get_single_cached_result(self, *, code: str, stdin_text: str) -> Optional[Dict[str, Any]]:
        cache_key = self._make_cache_key(code=code, case={"stdin": stdin_text})
        return self._cache_get(cache_key)

    def put_single_result(self, *, code: str, stdin_text: str, payload: Dict[str, Any]) -> None:
        if not str(stdin_text or "").strip():
            return
        cache_key = self._make_cache_key(code=code, case={"stdin": stdin_text})
        self._cache_put(cache_key, dict(payload or {}))
