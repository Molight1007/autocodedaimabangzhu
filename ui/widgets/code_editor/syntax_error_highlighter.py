"""Python 语法 / lint 错误高亮器。

职责：
- ast.parse() 捕获结构语法错误（Python 3.10+ PEG 解析器，位置精准）。
- pyflakes 捕获逻辑错误（未定义变量、未使用导入等）。
- 500ms 防抖延迟，通过 editor._syntax_error_selections 渲染。

不负责：
- 不修改文档内容，不影响其他组件。
"""

import ast
import io

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QColor, QTextCharFormat

try:
    from pyflakes.api import check
    from pyflakes.reporter import Reporter
    _PYFLAKES_OK = True
except ImportError:
    Reporter = object
    _PYFLAKES_OK = False


def _make_error_format() -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setUnderlineColor(QColor("#e51400"))
    fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
    return fmt


def _should_keep_pyflakes_message(message) -> bool:
    """兼容不同 pyflakes 版本的消息标识字段。"""
    keep_ids = {"F821", "F822", "F823", "F831"}
    msg_id = getattr(message, "message_id", None)
    if msg_id in keep_ids:
        return True

    keep_types = {
        "UndefinedName",
        "UndefinedExport",
        "UndefinedLocal",
        "DuplicateArgument",
    }
    return type(message).__name__ in keep_types


class _CaptureReporter(Reporter):
    """自定义 Reporter：将 pyflakes 消息收集到列表。"""
    def __init__(self):
        self._out = io.StringIO()
        super().__init__(self._out, self._out)
        self.errors = []

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.errors.append({
            "source": "pyflakes",
            "lineno": lineno,
            "col": (offset or 1) - 1,
            "msg": str(msg or ""),
        })

    def flake(self, message):
        # 只保留关键错误，过滤掉编写过程中的噪音
        if not _should_keep_pyflakes_message(message):
            return
        self.errors.append({
            "source": "pyflakes",
            "lineno": getattr(message, "lineno", 1),
            "col": max((getattr(message, "col", None) or 1) - 1, 0),
            "msg": str(message),
        })


class SyntaxErrorHighlighter(QObject):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._editor._syntax_error_selections = []
        self._enabled = True

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._run_check)

        editor.textChanged.connect(self._schedule_check)
        if hasattr(editor, "contentMutated"):
            editor.contentMutated.connect(self._schedule_check)

    def _schedule_check(self):
        if not self._enabled:
            return
        self._timer.start()

    def _run_check(self):
        if not self._enabled:
            return
        source = self._editor.toPlainText()
        selections = []
        if source.strip():
            # 第一层：ast.parse —— Python 3.10+ PEG 解析器，对位置精准
            try:
                ast.parse(source)
            except SyntaxError as e:
                selections = self._build_selections(e)

            # 第二层：pyflakes —— 语法无误时检查逻辑错误
            if not selections and _PYFLAKES_OK:
                selections = self._run_pyflakes(source)

        self._editor._syntax_error_selections = selections
        self._editor.highlight_current_line()

    def disable(self):
        self._enabled = False
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self._editor.textChanged.disconnect(self._schedule_check)
        except Exception:
            pass
        try:
            if hasattr(self._editor, "contentMutated"):
                self._editor.contentMutated.disconnect(self._schedule_check)
        except Exception:
            pass
        self._editor._syntax_error_selections = []
        self._editor.highlight_current_line()

    # ── ast.parse 语法错误 ─────────────────────────────────

    def _build_selections(self, e: SyntaxError):
        """将 SyntaxError 转为 [(abs_pos, length)]。"""
        doc = self._editor.document()
        start_line = max((e.lineno or 1) - 1, 0)
        end_line = max((getattr(e, "end_lineno", None) or e.lineno or 1) - 1, start_line)
        start_col = max((e.offset or 1) - 1, 0)
        end_col = getattr(e, "end_offset", None)
        if end_col is None:
            end_col = start_col + 1

        result = []
        for line_num in range(start_line, end_line + 1):
            block = doc.findBlockByNumber(line_num)
            if not block.isValid():
                continue
            block_pos = block.position()
            text = block.text()
            if line_num == start_line and line_num == end_line:
                length = max(end_col - start_col, 1)
                result.append((block_pos + start_col, length))
            elif line_num == start_line:
                result.append((block_pos + start_col, max(len(text) - start_col, 1)))
            elif line_num == end_line:
                result.append((block_pos, min(end_col, len(text))))
            else:
                result.append((block_pos, len(text)))
        return result

    # ── pyflakes lint ─────────────────────────────────────

    def _run_pyflakes(self, source):
        reporter = _CaptureReporter()
        # pyflakes 内部 compile，但已通过 ast.parse，不会再报 SyntaxError
        check(source, "<input>", reporter)
        return self._build_pyflakes_selections(reporter.errors)

    def _build_pyflakes_selections(self, errors):
        doc = self._editor.document()
        result = []
        for err in errors:
            line_num = max(err["lineno"] - 1, 0)
            col = err["col"]
            block = doc.findBlockByNumber(line_num)
            if not block.isValid():
                continue
            block_pos = block.position()
            text = block.text()

            # 找到该列对应的 token 长度
            token_start = col
            token_end = col
            while token_end < len(text) and (text[token_end].isalnum() or text[token_end] == "_"):
                token_end += 1
            length = max(token_end - token_start, 1)

            result.append((block_pos + token_start, length))
        return result
