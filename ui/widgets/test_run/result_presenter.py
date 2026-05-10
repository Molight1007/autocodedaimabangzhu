from __future__ import annotations

import json
from typing import Any, Dict


def format_case_preview(case: Dict[str, Any]) -> str:
    try:
        text = json.dumps(case or {}, ensure_ascii=False, indent=2)
    except Exception:
        text = repr(case or {})
    if len(text) > 12000:
        return text[:12000] + "\n...\n"
    return text


def format_output_text(*, stdout: str, stderr: str, timed_out: bool, timeout_sec: float) -> str:
    out = str(stdout or "")
    err = str(stderr or "")
    shown = out
    if err.strip():
        shown = (out + ("\n" if out and err else "") + err).rstrip() + "\n"
    if bool(timed_out):
        shown = (shown.rstrip() + f"\n\ntimeout>{float(timeout_sec)}s\n").rstrip() + "\n"
    return shown
