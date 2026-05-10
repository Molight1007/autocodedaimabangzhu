from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from services.update import update_config


@dataclass
class UpdateInfo:
    latest_version: str
    notes: str
    download_url: str
    published_at: Optional[str] = None


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _safe_parse_json(text: str) -> dict:
    try:
        data = json.loads(text or "")
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _parse_version(version: str) -> tuple:
    parts = []
    for part in str(version or "").strip().split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts or [0])


def is_newer_version(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def get_current_version() -> str:
    root = _project_root()
    local_path = os.path.join(root, update_config.LOCAL_VERSION_FILE)
    if os.path.exists(local_path):
        try:
            text = ""
            with open(local_path, "r", encoding="utf-8") as f:
                text = f.read()
            data = _safe_parse_json(text)
            v = str(data.get("version") or "").strip()
            if v:
                return v
        except Exception:
            pass
    return str(update_config.CURRENT_VERSION or "").strip() or "0.0.0"


def fetch_update_info() -> Optional[UpdateInfo]:
    url = str(update_config.UPDATE_FEED_URL or "").strip()
    if not url:
        return None
    try:
        req = Request(url, headers={"User-Agent": "DataGenerator"})
        with urlopen(req, timeout=update_config.UPDATE_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except URLError:
        return None
    except Exception:
        return None

    data = _safe_parse_json(text)
    latest_version = str(data.get("version") or "").strip()
    download_url = str(data.get("download_url") or "").strip()
    if not latest_version or not download_url:
        return None

    notes = str(data.get("notes") or "").strip()
    published_at = str(data.get("published_at") or "").strip() or None
    return UpdateInfo(
        latest_version=latest_version,
        notes=notes,
        download_url=download_url,
        published_at=published_at,
    )
