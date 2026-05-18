"""括号与引号配对高亮器。

职责：
- 检测光标附近的括号 () [] {} 和引号 ' " 并高亮匹配对。
- 正确处理三引号 的配对（光标在任一字符上均可识别）。
- 通过 editor._bracket_match_selections 与 highlight_current_line 协作。

不负责：
- 不修改文档内容，不影响编辑器其他功能。
"""

from PySide6.QtCore import QObject

OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}
QUOTES = {"'", '"'}


def _is_escaped(text: str, pos: int) -> bool:
    """检查 text[pos] 是否被反斜杠转义。"""
    count = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _get_triple_start(text: str, pos: int, quote_char: str) -> int | None:
    """若 pos 属于 三引号 序列，返回序列的起始位置；否则返回 None。"""
    triple = quote_char * 3
    for offset in (0, -1, -2):
        start = pos + offset
        if 0 <= start and start + 3 <= len(text) and text[start:start + 3] == triple:
            return start
    return None


class BracketMatchHighlighter(QObject):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._editor._bracket_match_selections = []
        self._editor.cursorPositionChanged.connect(self._on_cursor_moved)

    def _on_cursor_moved(self):
        pairs = self._find_match()
        self._editor._bracket_match_selections = pairs
        self._editor.highlight_current_line()

    def _find_match(self):
        cursor = self._editor.textCursor()
        pos = cursor.position()
        text = self._editor.toPlainText()
        if not text or pos > len(text):
            return []

        # 检查光标处和光标前一个字符
        for offset in (0, -1):
            check = pos + offset
            if 0 <= check < len(text):
                ch = text[check]

                if ch in OPENERS:
                    result = self._scan(text, check, 1, OPENERS[ch])
                    if result:
                        return result

                if ch in CLOSERS:
                    result = self._scan(text, check, -1, CLOSERS[ch])
                    if result:
                        return result

                if ch in QUOTES:
                    result = self._match_quote(text, check, ch)
                    if result:
                        return result

        return []

    # ── 括号扫描 ────────────────────────────────────────────

    def _scan(self, text, start, direction, target):
        """通用扫描：从 start 沿 direction 找 target，跳过嵌套对。"""
        if direction > 0:
            close = target
            open_pair = CLOSERS[close]
        else:
            open_pair = target
            close = OPENERS[open_pair]

        if direction > 0:
            nested_bump = open_pair
            match_char = close
        else:
            nested_bump = close
            match_char = open_pair

        depth = 1
        i = start + direction
        while 0 <= i < len(text):
            ch = text[i]
            if ch == nested_bump:
                depth += 1
            elif ch == match_char:
                depth -= 1
                if depth == 0:
                    if direction > 0:
                        return [(start, 1), (i, 1)]
                    else:
                        return [(i, 1), (start, 1)]
            i += direction
        return None

    # ── 引号匹配 ────────────────────────────────────────────

    def _match_quote(self, text, pos, quote_char):
        """判断引号类型（单/三重），找到配对。"""
        triple_start = _get_triple_start(text, pos, quote_char)
        if triple_start is not None:
            return self._match_triple(text, triple_start, quote_char)

        return self._match_single(text, pos, quote_char)

    # ── 单引号 ──────────────────────────────────────────────

    def _match_single(self, text, pos, quote_char):
        """匹配单引号（不属三引号序列的 ' 或 "）。"""
        count_before = 0
        i = pos - 1
        while i >= 0:
            if text[i] == quote_char and not _is_escaped(text, i):
                if _get_triple_start(text, i, quote_char) is None:
                    count_before += 1
            i -= 1

        if count_before % 2 == 0:
            return self._scan_single(text, pos, 1, quote_char)
        else:
            return self._scan_single(text, pos, -1, quote_char)

    def _scan_single(self, text, start, direction, quote_char):
        """扫描匹配的单引号，略过三引号序列中的字符。"""
        i = start + direction
        while 0 <= i < len(text):
            if text[i] == quote_char and not _is_escaped(text, i):
                if _get_triple_start(text, i, quote_char) is None:
                    if direction > 0:
                        return [(start, 1), (i, 1)]
                    else:
                        return [(i, 1), (start, 1)]
            i += direction
        return None

    # ── 三引号 ──────────────────────────────────────────────

    def _match_triple(self, text, start, quote_char):
        """匹配三引号：统计之前的三引号数量，决定搜索方向。"""
        triple = quote_char * 3
        count_before = 0
        i = start - 1
        while i >= 0:
            if text[i:i + 3] == triple and not _is_escaped(text, i):
                count_before += 1
                i -= 3  # 跳过整个三引号序列
            else:
                i -= 1

        if count_before % 2 == 0:
            return self._scan_triple(text, start, 1, quote_char)
        else:
            return self._scan_triple(text, start, -1, quote_char)

    def _scan_triple(self, text, start, direction, quote_char):
        """扫描匹配的三引号序列，返回 [(start, 3), (match, 3)]。"""
        triple = quote_char * 3
        i = start + (3 if direction > 0 else -1)
        while 0 <= i < len(text) - 2:
            if text[i:i + 3] == triple and not _is_escaped(text, i):
                if direction > 0:
                    return [(start, 3), (i, 3)]
                else:
                    return [(i, 3), (start, 3)]
            i += direction
        return None
