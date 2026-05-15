"""验证层追踪日志工具。

为 cross_runner 提供：
- JSONL 格式事件日志写入
- 内容裁剪（防止超大字典撑爆日志文件）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def _resolve_trace_path() -> Path:
    root_dir = Path(__file__).resolve().parents[3]
    return root_dir / "logs" / "verifier_trace.jsonl"


def append_verifier_trace(event: str, payload: Dict[str, Any]) -> None:
    try:
        path = _resolve_trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": str(event or ""),
            "payload": clip_json(payload),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
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
