from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Dict, Optional


class ResultCache:
    def __init__(self, *, max_items: int = 2000):
        self._max_items = max(1, int(max_items or 1))
        self._data: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def make_key(self, *, code: str, case: Dict[str, Any]) -> str:
        try:
            case_json = json.dumps(case or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        except Exception:
            case_json = repr(case or {})
        h = hashlib.sha256()
        h.update((code or "").encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update(case_json.encode("utf-8", errors="ignore"))
        return h.hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        payload = self._data.get(key)
        if payload is None:
            return None
        try:
            self._data.move_to_end(key)
        except Exception:
            pass
        return dict(payload or {})

    def put(self, key: str, payload: Dict[str, Any]) -> None:
        if not key:
            return
        self._data[key] = dict(payload or {})
        try:
            self._data.move_to_end(key)
        except Exception:
            pass
        while len(self._data) > self._max_items:
            try:
                self._data.popitem(last=False)
            except Exception:
                break

    def clear(self) -> None:
        try:
            self._data.clear()
        except Exception:
            pass
