from __future__ import annotations

import os
import time
import zipfile
from typing import Any, Dict, Iterable, Optional, Tuple, Union

from PySide6.QtCore import QSettings


def get_output_dir() -> str:
    settings = QSettings("DataGenerator", "Config")
    v = settings.value("gen/output_dir", "")
    return str(v or "").strip()


def export_tests_zip(
    *,
    output_dir: str,
    cases: Iterable[Dict[str, Any]],
    run_payloads: Optional[Iterable[Dict[str, Any]]] = None,
    extra_files: Optional[Dict[str, Union[str, bytes]]] = None,
    zip_basename: Optional[str] = None,
) -> str:
    out_dir = str(output_dir or "").strip()
    if not out_dir:
        raise ValueError("empty_output_dir")

    os.makedirs(out_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    name = (zip_basename or "").strip()
    if not name:
        name = f"tests_{ts}.zip"
    if not name.lower().endswith(".zip"):
        name += ".zip"

    zip_path = os.path.join(out_dir, name)
    runs = list(run_payloads or [])
    extra = dict(extra_files or {})

    pairs: list[Tuple[str, str]] = []
    cases_list = list(cases or [])
    for i, c in enumerate(cases_list):
        case = dict(c or {})
        stdin_text = "" if case.get("stdin") is None else str(case.get("stdin") or "")
        if stdin_text and not stdin_text.endswith("\n"):
            stdin_text += "\n"

        out_text = "" if case.get("stdout") is None else str(case.get("stdout") or "")
        if i < len(runs):
            r = dict(runs[i] or {})
            stdout_run = str(r.get("stdout") or "")
            stderr_run = str(r.get("stderr") or "")
            if stdout_run.strip():
                out_text = stdout_run
            elif stderr_run.strip():
                out_text = stderr_run

        if out_text and not out_text.endswith("\n"):
            out_text += "\n"

        pairs.append((stdin_text, out_text))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for idx, (stdin_text, out_text) in enumerate(pairs, start=1):
            zf.writestr(f"test{idx}.in", stdin_text)
            zf.writestr(f"test{idx}.out", out_text)

        for rel, content in extra.items():
            rel_path = str(rel or "").replace("\\", "/").lstrip("/")
            if not rel_path:
                continue
            if isinstance(content, bytes):
                data = content
            else:
                data = str(content or "").encode("utf-8")
            zf.writestr(rel_path, data)

    return zip_path

