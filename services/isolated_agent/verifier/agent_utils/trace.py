"""聚合追踪日志工具。

统一为单 Agent 与双算法对拍提供：
- 按问题聚合的 JSON 数组日志
- 单问题归档文件
- 内容裁剪与代码版本摘要工具
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


CODE_FIELD_NAMES = {
    "code",
    "code_a",
    "code_b",
    "final_code",
    "current_code",
    "candidate_code",
}

CASE_FIELD_NAMES = {
    "cases",
    "mismatch_cases",
}


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_logs_dir() -> Path:
    root_dir = Path(__file__).resolve().parents[4]
    return root_dir / "logs"


def _resolve_trace_path() -> Path:
    return _resolve_logs_dir() / "agent_trace.json"


def _resolve_run_dir() -> Path:
    return _resolve_logs_dir() / "agent_runs"


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _code_digest(code: str) -> str:
    text = str(code or "")
    if not text:
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _load_trace_rows() -> List[Dict[str, Any]]:
    path = _resolve_trace_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _save_trace_rows(rows: List[Dict[str, Any]]) -> None:
    _atomic_write_json(_resolve_trace_path(), rows)


def _compact_case_payload(case_item: Any) -> Dict[str, Any]:
    one = dict(case_item or {}) if isinstance(case_item, dict) else {"value": case_item}
    stdin_text = str(one.get("stdin") or "")
    out_a = str(one.get("out_a") or "")
    out_b = str(one.get("out_b") or "")
    return {
        "index": int(one.get("index") or 0),
        "match": bool(one.get("match")),
        "stdin_preview": clip_json(stdin_text, limit=400),
        "stdin_len": len(stdin_text),
        "out_a_preview": clip_json(out_a, limit=300),
        "out_a_len": len(out_a),
        "out_b_preview": clip_json(out_b, limit=300),
        "out_b_len": len(out_b),
        "error_a": clip_json(str(one.get("error_a") or ""), limit=300),
        "error_b": clip_json(str(one.get("error_b") or ""), limit=300),
        "skip_reason": str(one.get("skip_reason") or ""),
    }


def _compact_code_text(value: Any) -> Dict[str, Any]:
    code_text = str(value or "")
    return {
        "hash": _code_digest(code_text),
        "chars": len(code_text),
        "lines": code_text.count("\n") + (1 if code_text else 0),
    }


def _compact_for_event(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        compact: Dict[str, Any] = {}
        for sub_key, sub_value in value.items():
            normalized_key = str(sub_key)
            if normalized_key in CODE_FIELD_NAMES:
                compact[normalized_key] = _compact_code_text(sub_value)
                continue
            if normalized_key in CASE_FIELD_NAMES and isinstance(sub_value, list):
                compact[normalized_key] = {
                    "count": len(sub_value),
                    "items": [_compact_case_payload(item) for item in sub_value[:1]],
                }
                continue
            compact[normalized_key] = _compact_for_event(sub_value, key=normalized_key)
        return compact
    if isinstance(value, list):
        shown = [_compact_for_event(item, key=key) for item in value[:3]]
        if len(value) > 3:
            shown.append({"truncated_items": len(value) - 3})
        return shown
    if key in CODE_FIELD_NAMES:
        return _compact_code_text(value)
    text = str(value)
    if len(text) <= 1200:
        return value
    return clip_json(text, limit=1200)


def _compact_problem(problem: Dict[str, Any]) -> Dict[str, Any]:
    source = dict(problem or {})
    return {
        "title": clip_json(str(source.get("title") or ""), limit=500),
        "description": clip_json(str(source.get("description") or ""), limit=1000),
        "constraints": clip_json(str(source.get("constraints") or ""), limit=600),
        "has_cross_feedback": bool(source.get("cross_feedback")),
        "sample_count": len(list(source.get("data_samples") or source.get("samples") or source.get("cases") or [])),
        "other_keys": sorted(
            str(key)
            for key in source.keys()
            if str(key) not in {"title", "description", "constraints", "data_samples", "samples", "cases", "cross_feedback"}
        ),
    }


def _compact_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    source = dict(summary or {})
    mismatch_cases = list(source.get("mismatch_cases") or [])
    passed = source.get("passed")
    if passed is None:
        passed = source.get("verification_passed")
    return {
        "passed": bool(passed),
        "strategy": str(source.get("strategy") or ""),
        "reason": str(source.get("reason") or ""),
        "skip_reason": str(source.get("skip_reason") or ""),
        "error": clip_json(str(source.get("error") or ""), limit=800),
        "total_cases": int(source.get("total_cases") or 0),
        "matched_cases": int(source.get("matched_cases") or 0),
        "a_success": bool(source.get("a_success")),
        "b_success": bool(source.get("b_success")),
        "a_rounds": int(source.get("a_rounds") or 0),
        "b_rounds": int(source.get("b_rounds") or 0),
        "first_mismatch": _compact_case_payload(mismatch_cases[0]) if mismatch_cases else {},
        "analysis": _compact_for_event(source.get("analysis") or {}),
    }


def _build_run_snapshot(
    *,
    run_id: str,
    problem: Dict[str, Any],
    mode: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    now = _now_text()
    return {
        "run_id": str(run_id or _new_run_id()),
        "status": "running",
        "mode": str(mode or ""),
        "started_at": now,
        "updated_at": now,
        "finished_at": "",
        "problem": _compact_problem(problem),
        "meta": _compact_for_event(dict(meta or {})),
        "message": [],
        "code": [],
        "summary": {},
    }


def _find_run_index(rows: List[Dict[str, Any]], run_id: str) -> int:
    for idx, row in enumerate(rows):
        if str(row.get("run_id") or "") == str(run_id or ""):
            return idx
    return -1


def _save_run_archive(row: Dict[str, Any]) -> None:
    run_id = str((row or {}).get("run_id") or "").strip()
    if not run_id:
        return
    archive_path = _resolve_run_dir() / f"{run_id}.json"
    _atomic_write_json(archive_path, row)


def start_agent_trace_run(
    *,
    problem: Dict[str, Any],
    mode: str,
    meta: Dict[str, Any],
) -> str:
    try:
        rows = _load_trace_rows()
        run_id = _new_run_id()
        row = _build_run_snapshot(
            run_id=run_id,
            problem=dict(problem or {}),
            mode=str(mode or ""),
            meta=dict(meta or {}),
        )
        rows.append(row)
        _save_trace_rows(rows)
        _save_run_archive(row)
        return run_id
    except Exception:
        return ""


def append_agent_trace_message(run_id: str, payload: Dict[str, Any]) -> None:
    try:
        message_text = str((payload or {}).get("message") or "").strip()
        if not message_text:
            return
        rows = _load_trace_rows()
        idx = _find_run_index(rows, run_id)
        if idx < 0:
            return
        row = dict(rows[idx])
        row.setdefault("message", [])
        ts = _now_text()
        item = {
            "ts": ts,
            "stage": str((payload or {}).get("stage") or ""),
            "message": message_text,
        }
        for key in ("slot", "algorithm", "action", "attempt", "result_type", "current", "total", "matched", "mode", "version"):
            if key in (payload or {}):
                item[key] = deepcopy((payload or {}).get(key))
        details = dict((payload or {}).get("details") or {})
        if details:
            item["details"] = _compact_for_event(details)
        row["updated_at"] = ts
        row["message"].append(item)
        rows[idx] = row
        _save_trace_rows(rows)
        _save_run_archive(row)
    except Exception:
        return


def append_agent_trace_code_version(
    run_id: str,
    *,
    slot: str,
    phase: str,
    code: str,
    success: bool,
    rounds: int,
    error: str = "",
    extra: Dict[str, Any] | None = None,
) -> None:
    try:
        code_text = str(code or "")
        if not code_text:
            return
        rows = _load_trace_rows()
        idx = _find_run_index(rows, run_id)
        if idx < 0:
            return
        row = dict(rows[idx])
        row.setdefault("code", [])
        ts = _now_text()
        row["updated_at"] = ts
        row["code"].append(
            {
                "ts": ts,
                "slot": str(slot or ""),
                "phase": str(phase or ""),
                "success": bool(success),
                "rounds": int(rounds or 0),
                "error": str(error or ""),
                "hash": _code_digest(code_text),
                "chars": len(code_text),
                "lines": code_text.count("\n") + (1 if code_text else 0),
                "content": code_text,
                "extra": _compact_for_event(dict(extra or {})),
            }
        )
        rows[idx] = row
        _save_trace_rows(rows)
        _save_run_archive(row)
    except Exception:
        return


def finalize_agent_trace_run(run_id: str, summary: Dict[str, Any], *, status: str = "done") -> None:
    try:
        rows = _load_trace_rows()
        idx = _find_run_index(rows, run_id)
        if idx < 0:
            return
        row = dict(rows[idx])
        now = _now_text()
        row["updated_at"] = now
        row["finished_at"] = now
        row["status"] = str(status or "done")
        row["summary"] = _compact_summary(dict(summary or {}))
        rows[idx] = row
        _save_trace_rows(rows)
        _save_run_archive(row)
    except Exception:
        return


def clip_json(value: Any, limit: int = 3000) -> Any:
    if isinstance(value, dict):
        return {str(k): clip_json(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [clip_json(x, limit=limit) for x in value]
    text = str(value)
    if len(text) <= limit:
        return value
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + "\n...\n(trace truncated)\n...\n" + text[-tail:]
