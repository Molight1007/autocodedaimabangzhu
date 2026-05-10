from __future__ import annotations

from typing import Optional, Tuple


class StdinHistoryController:
    def __init__(self, *, max_items: int):
        self._max_items = max(1, int(max_items or 1))
        self._items: list[str] = []
        self._pos: int = -1

    def push(self, text: str) -> None:
        t = str(text or "")
        if not t.strip():
            return
        self._items = [x for x in self._items if x != t]
        self._items.append(t)
        self._items = self._items[-self._max_items :]
        self._pos = len(self._items) - 1

    def ui_state(self) -> Tuple[str, bool, bool]:
        total = len(self._items)
        if total <= 0 or self._pos < 0:
            return "0/0", False, False
        return f"{self._pos + 1}/{total}", self._pos > 0, self._pos < total - 1

    def prev_text(self) -> Optional[str]:
        if self._pos <= 0:
            return None
        self._pos -= 1
        return self._items[self._pos]

    def next_text(self) -> Optional[str]:
        total = len(self._items)
        if total <= 0:
            return None
        if self._pos < 0:
            return None
        if self._pos >= total - 1:
            return None
        self._pos += 1
        return self._items[self._pos]

    def clear(self) -> None:
        self._items = []
        self._pos = -1
