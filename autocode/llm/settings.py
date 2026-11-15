"""本地 LLM 设置：界面写入 `data/settings.json`，优先级高于环境变量。

字段：api_key / base_url / model。文件权限尽量设为 600；
密钥只在本机保存，接口返回时永远打码。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_ENV_FILE = "AUTOCODE_SETTINGS_FILE"


def settings_path() -> Path:
    override = os.environ.get(_ENV_FILE, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "settings.json"


def load_settings() -> Dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(payload: Dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def effective() -> Dict[str, Any]:
    """当前生效值（不含优先级判断的原始来源，供 UI 展示）。"""
    data = load_settings()
    return {
        "base_url": str(data.get("base_url") or "").strip(),
        "model": str(data.get("model") or "").strip(),
        "api_key": str(data.get("api_key") or "").strip(),
    }


def mask(key: str) -> str:
    key = str(key or "")
    if len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}…{key[-4:]}"


def update_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按字段更新；api_key 传空字符串表示"保持不变"，clear_key=true 才清空。"""
    data = load_settings()
    for field in ("base_url", "model"):
        if field in payload and payload[field] is not None:
            data[field] = str(payload[field]).strip()
    if payload.get("clear_key"):
        data.pop("api_key", None)
    elif payload.get("api_key"):
        data["api_key"] = str(payload["api_key"]).strip()
    from datetime import datetime, timezone

    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_settings(data)
    return data
