"""标识符同名高亮器。

职责：
- 当光标位于标识符上时，高亮全文中所有同名标识符。
- 仅做文本级标识符匹配，不做作用域/语义分析。
- 通过 editor._symbol_reference_selections 与 highlight_current_line 协作。
"""

from __future__ import annotations

import keyword
import re

from PySide6.QtCore import QObject


def _extract_identifier(text: str, pos: int) -> str:
    if not text or pos < 0:
        return ""
    if pos >= len(text):
        pos = len(text) - 1
    if pos < 0:
        return ""

    if not (text[pos].isalnum() or text[pos] == "_"):
        return ""

    start = pos
    end = pos + 1
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    ident = text[start:end]
    if not ident.isidentifier() or keyword.iskeyword(ident):
        return ""
    return ident


def _build_occurrences(text: str, ident: str) -> list[tuple[int, int]]:
    if not text or not ident:
        return []
    pattern = re.compile(r"(?<!\w)" + re.escape(ident) + r"(?!\w)")
    return [(m.start(), m.end() - m.start()) for m in pattern.finditer(text)]


class SymbolReferenceHighlighter(QObject):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._enabled = True
        self._editor._symbol_reference_selections = []
        self._editor.cursorPositionChanged.connect(self._on_cursor_moved)

    def _on_cursor_moved(self):
        if not self._enabled:
            return
        self._editor._symbol_reference_selections = self._find_matches()
        self._editor.highlight_current_line()

    def disable(self):
        self._enabled = False
        try:
            self._editor.cursorPositionChanged.disconnect(self._on_cursor_moved)
        except Exception:
            pass
        self._editor._symbol_reference_selections = []
        self._editor.highlight_current_line()

    def _find_matches(self):
        cursor = self._editor.textCursor()
        text = self._editor.toPlainText()
        if not text:
            return []

        selected = cursor.selectedText().replace("\u2029", "\n")
        ident = ""
        if selected and "\n" not in selected:
            candidate = selected.strip()
            if candidate.isidentifier() and not keyword.iskeyword(candidate):
                ident = candidate

        if not ident:
            for offset in (0, -1):
                ident = _extract_identifier(text, cursor.position() + offset)
                if ident:
                    break

        if not ident:
            return []
        return _build_occurrences(text, ident)
