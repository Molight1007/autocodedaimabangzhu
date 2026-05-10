from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QTextCharFormat, QSyntaxHighlighter, QFont

from ...utils.python_keywords import KEYWORDS, BUILTINS


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)

        self._rules = []

        def fmt(color: str, bold: bool = False, italic: bool = False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            if italic:
                f.setFontItalic(True)
            return f

        keyword_format = fmt("#0056D6", bold=True)
        builtin_format = fmt("#7B1FA2")
        number_format = fmt("#D32F2F")
        string_format = fmt("#1B5E20")
        comment_format = fmt("#6A9955", italic=True)
        decorator_format = fmt("#FF8F00")

        for kw in KEYWORDS:
            self._rules.append((QRegularExpression(rf"\b{kw}\b"), keyword_format))

        for bi in BUILTINS:
            self._rules.append((QRegularExpression(rf"\b{bi}\b"), builtin_format))

        self._rules.append((QRegularExpression(r"\b\d+(\.\d+)?\b"), number_format))
        self._rules.append((QRegularExpression(r"#[^\n]*"), comment_format))
        self._rules.append((QRegularExpression(r"@[A-Za-z_]\w*"), decorator_format))

        self._rules.append((QRegularExpression(r"(?<!\\)'([^'\\]|\\.)*'"), string_format))
        self._rules.append((QRegularExpression(r'(?<!\\)"([^"\\]|\\.)*"'), string_format))

        self._triple_single = QRegularExpression(r"'''")
        self._triple_double = QRegularExpression(r'"""')
        self._triple_string_format = string_format

    def highlightBlock(self, text: str):
        for pattern, char_format in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), char_format)

        self._highlight_multiline_strings(text)

    def _highlight_multiline_strings(self, text: str):
        in_single = self.previousBlockState() == 1
        in_double = self.previousBlockState() == 2

        if not in_single and not in_double:
            start_single = text.find("'''")
            start_double = text.find('"""')
            if start_single == -1 and start_double == -1:
                self.setCurrentBlockState(0)
                return

        if in_single:
            start = 0
            end = text.find("'''")
            if end == -1:
                self.setFormat(0, len(text), self._triple_string_format)
                self.setCurrentBlockState(1)
                return
            self.setFormat(0, end + 3, self._triple_string_format)
            self.setCurrentBlockState(0)
            return

        if in_double:
            start = 0
            end = text.find('"""')
            if end == -1:
                self.setFormat(0, len(text), self._triple_string_format)
                self.setCurrentBlockState(2)
                return
            self.setFormat(0, end + 3, self._triple_string_format)
            self.setCurrentBlockState(0)
            return

        start_single = text.find("'''")
        start_double = text.find('"""')
        if start_single != -1 and (start_double == -1 or start_single < start_double):
            end = text.find("'''", start_single + 3)
            if end == -1:
                self.setFormat(start_single, len(text) - start_single, self._triple_string_format)
                self.setCurrentBlockState(1)
                return
            self.setFormat(start_single, end - start_single + 3, self._triple_string_format)
            self.setCurrentBlockState(0)
            return

        if start_double != -1:
            end = text.find('"""', start_double + 3)
            if end == -1:
                self.setFormat(start_double, len(text) - start_double, self._triple_string_format)
                self.setCurrentBlockState(2)
                return
            self.setFormat(start_double, end - start_double + 3, self._triple_string_format)
            self.setCurrentBlockState(0)
            return
