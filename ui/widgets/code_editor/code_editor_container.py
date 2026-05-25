from typing import Any, Optional, Dict

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
)

from .code_editor import CodeEditor
from .python_syntax_highlighter import PythonSyntaxHighlighter
from .python_code_completer import PythonCodeCompleter
from .bracket_match_highlighter import BracketMatchHighlighter
from .symbol_reference_highlighter import SymbolReferenceHighlighter
from .syntax_error_highlighter import SyntaxErrorHighlighter


# code_editor_container.py 中修改
class CodeEditorContainer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.editor = CodeEditor()
        self.layout.addWidget(self.editor)

        self._highlighter = None
        self._completer = None
        self._bracket_matcher = None
        self._symbol_reference_hl = None
        self._syntax_error_hl = None
        self._dispatch_api = None
        self._find_replace_enabled = False
        self._find_shortcut = None
        self._replace_shortcut = None
        self._close_find_shortcut = None

        self._highlighter = PythonSyntaxHighlighter(self.editor.document())
        self._completer = PythonCodeCompleter(self.editor, self)
        self._bracket_matcher = BracketMatchHighlighter(self.editor, self)
        self._symbol_reference_hl = SymbolReferenceHighlighter(self.editor, self)
        self._syntax_error_hl = SyntaxErrorHighlighter(self.editor, self)

        # 关键：将补全器引用传递给编辑器
        self.editor._completer = self._completer  # 直接设置属性
        self.editor.installEventFilter(self)
        self._build_find_replace_panel()
        self._reposition_find_replace_panel()

    def eventFilter(self, obj, event):
        if obj is self.editor and event.type() in {QEvent.Resize, QEvent.Show, QEvent.Move}:
            self._reposition_find_replace_panel()
        return super().eventFilter(obj, event)

    def _build_find_replace_panel(self):
        self._find_panel = QFrame(self.editor)
        self._find_panel.setObjectName("FindReplacePanel")
        self._find_panel.setStyleSheet(
            """
            QFrame#FindReplacePanel {
                background: #ffffff;
                border: 1px solid #d0d7de;
                border-radius: 6px;
            }
            QLineEdit {
                min-height: 28px;
                padding: 4px 6px;
                border: 1px solid #c8cdd4;
                border-radius: 4px;
                background: #ffffff;
            }
            QPushButton {
                min-height: 28px;
                padding: 0 10px;
            }
            """
        )
        panel_layout = QVBoxLayout(self._find_panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)
        panel_layout.setSpacing(6)

        find_row = QHBoxLayout()
        find_row.setSpacing(6)
        find_row.addWidget(QLabel("查找"))

        self._find_input = QLineEdit(self._find_panel)
        self._find_input.setPlaceholderText("输入要查找的文本")
        find_row.addWidget(self._find_input, 1)

        self._case_checkbox = QCheckBox("区分大小写", self._find_panel)
        find_row.addWidget(self._case_checkbox)

        self._btn_prev = QPushButton("上一个", self._find_panel)
        self._btn_prev.clicked.connect(self._find_previous)
        find_row.addWidget(self._btn_prev)

        self._btn_next = QPushButton("下一个", self._find_panel)
        self._btn_next.clicked.connect(self._find_next)
        find_row.addWidget(self._btn_next)

        self._btn_close_find = QPushButton("关闭", self._find_panel)
        self._btn_close_find.clicked.connect(self._hide_find_replace_panel)
        find_row.addWidget(self._btn_close_find)
        panel_layout.addLayout(find_row)

        self._replace_row_widget = QWidget(self._find_panel)
        replace_row = QHBoxLayout(self._replace_row_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        replace_row.setSpacing(6)
        replace_row.addWidget(QLabel("替换"))

        self._replace_input = QLineEdit(self._replace_row_widget)
        self._replace_input.setPlaceholderText("输入替换后的文本")
        replace_row.addWidget(self._replace_input, 1)

        self._btn_replace = QPushButton("替换", self._replace_row_widget)
        self._btn_replace.clicked.connect(self._replace_one)
        replace_row.addWidget(self._btn_replace)

        self._btn_replace_all = QPushButton("全部替换", self._replace_row_widget)
        self._btn_replace_all.clicked.connect(self._replace_all)
        replace_row.addWidget(self._btn_replace_all)
        panel_layout.addWidget(self._replace_row_widget)

        self._find_input.returnPressed.connect(self._find_next)
        self._replace_input.returnPressed.connect(self._replace_one)
        self._find_panel.hide()

    def _reposition_find_replace_panel(self):
        panel = getattr(self, "_find_panel", None)
        if panel is None:
            return
        panel_width = min(620, max(360, self.editor.viewport().width() - 32))
        panel_height = panel.sizeHint().height()
        x = max(self.editor.line_number_area_width() + 8, self.editor.width() - panel_width - 12)
        y = 8
        panel.setGeometry(x, y, panel_width, panel_height)
        panel.raise_()

    def enable_find_replace(self):
        self._find_replace_enabled = True
        if self._find_shortcut is None:
            self._find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self.editor)
            self._find_shortcut.activated.connect(self.show_find_panel)
        if self._replace_shortcut is None:
            self._replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self.editor)
            self._replace_shortcut.activated.connect(self.show_replace_panel)
        if self._close_find_shortcut is None:
            self._close_find_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self._find_panel)
            self._close_find_shortcut.activated.connect(self._hide_find_replace_panel)
        self._find_shortcut.setEnabled(True)
        self._replace_shortcut.setEnabled(True)
        self._close_find_shortcut.setEnabled(True)

    def disable_find_replace(self):
        self._find_replace_enabled = False
        self._hide_find_replace_panel()
        for shortcut in (self._find_shortcut, self._replace_shortcut, self._close_find_shortcut):
            if shortcut is not None:
                shortcut.setEnabled(False)

    def show_find_panel(self):
        self._show_find_replace_panel(show_replace=False)

    def show_replace_panel(self):
        self._show_find_replace_panel(show_replace=True)

    def _show_find_replace_panel(self, *, show_replace: bool):
        if not self._find_replace_enabled:
            return
        self._replace_row_widget.setVisible(bool(show_replace))
        self._reposition_find_replace_panel()
        self._find_panel.show()
        self._find_panel.raise_()
        selected = self.editor.textCursor().selectedText().replace("\u2029", "\n")
        if selected and "\n" not in selected:
            self._find_input.setText(selected)
        self._find_input.setFocus()
        self._find_input.selectAll()

    def _hide_find_replace_panel(self):
        panel = getattr(self, "_find_panel", None)
        if panel is not None:
            panel.hide()
        self.editor.setFocus()

    def _find_flags(self, *, backward: bool = False):
        flags = QTextDocument.FindFlags()
        if backward:
            flags |= QTextDocument.FindBackward
        if self._case_checkbox.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        return flags

    def _set_find_input_error_state(self, has_error: bool):
        if has_error:
            self._find_input.setStyleSheet("border: 1px solid #d9534f; background: #fff3f2;")
            return
        self._find_input.setStyleSheet("")

    def _find_text(self, *, backward: bool = False):
        text = self._find_input.text()
        if not text:
            self._set_find_input_error_state(True)
            return False

        flags = self._find_flags(backward=backward)
        found = self.editor.find(text, flags)
        if not found:
            cursor = self.editor.textCursor()
            move_target = QTextCursor.End if backward else QTextCursor.Start
            cursor.movePosition(move_target)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(text, flags)
        self._set_find_input_error_state(not found)
        return bool(found)

    def _find_next(self):
        self._find_text(backward=False)

    def _find_previous(self):
        self._find_text(backward=True)

    def _selected_text_matches_find(self):
        text = self._find_input.text()
        if not text:
            return False
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return False
        selected = cursor.selectedText().replace("\u2029", "\n")
        if self._case_checkbox.isChecked():
            return selected == text
        return selected.casefold() == text.casefold()

    def _replace_one(self):
        if not self._find_input.text():
            self._set_find_input_error_state(True)
            return
        if not self._selected_text_matches_find() and not self._find_text(backward=False):
            return
        cursor = self.editor.textCursor()
        cursor.insertText(self._replace_input.text())
        self.editor.setTextCursor(cursor)
        self._find_next()

    def _replace_all(self):
        find_text = self._find_input.text()
        if not find_text:
            self._set_find_input_error_state(True)
            return
        replace_text = self._replace_input.text()
        doc = self.editor.document()
        flags = self._find_flags(backward=False)
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            work = doc.find(find_text, 0, flags)
            replaced = False
            while not work.isNull():
                work.insertText(replace_text)
                replaced = True
                start_pos = work.selectionStart()
                work = doc.find(find_text, start_pos, flags)
        finally:
            cursor.endEditBlock()
        self._set_find_input_error_state(not replaced)

    def set_highlighter(self, highlighter):
        self._highlighter = highlighter
        if self._highlighter is not None:
            self._highlighter.setDocument(self.editor.document())

    def set_completer(self, completer):
        self._completer = completer

    def disable_completer(self):
        c = self._completer
        if c is None:
            self.editor._completer = None
            return
        try:
            self.editor.removeEventFilter(c)
        except Exception:
            pass
        try:
            self.editor.textChanged.disconnect(c._on_text_changed)
        except Exception:
            pass
        try:
            if hasattr(c, "_popup_view") and c._popup_view is not None:
                c._popup_view.hide()
        except Exception:
            pass
        self._completer = None
        self.editor._completer = None

    def disable_highlighter(self):
        h = self._highlighter
        if h is None:
            return
        try:
            h.setDocument(None)
        except Exception:
            pass
        self._highlighter = None

    def disable_syntax_error_highlighter(self):
        h = self._syntax_error_hl
        if h is None:
            self.editor._syntax_error_selections = []
            self.editor.highlight_current_line()
            return
        try:
            h.disable()
        except Exception:
            pass
        self._syntax_error_hl = None

    def disable_symbol_reference_highlighter(self):
        h = self._symbol_reference_hl
        if h is None:
            self.editor._symbol_reference_selections = []
            self.editor.highlight_current_line()
            return
        try:
            h.disable()
        except Exception:
            pass
        self._symbol_reference_hl = None

    def disable_zoom(self):
        try:
            self.editor.disable_zoom()
        except Exception:
            pass

    def enable_zoom(self):
        try:
            self.editor.enable_zoom()
        except Exception:
            pass

    def get_text(self):
        return self.editor.toPlainText()

    def set_text(self, text):
        self.editor.setPlainText(text)

    def set_dispatch_api(self, dispatch_api: Any):
        self._dispatch_api = dispatch_api

    def get_dispatch_api(self) -> Any:
        return self._dispatch_api

    def reset_template(self, template_name: str, params: Optional[Dict[str, Any]] = None):
        from services.prompts.code_templates import get_template
        self.set_text(get_template(template_name, params=params))

    def request_ai(self, task_name: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if self._dispatch_api is None:
            raise RuntimeError("dispatch_api_not_set")
        if not hasattr(self._dispatch_api, "request"):
            raise RuntimeError("dispatch_api_missing_request")
        return self._dispatch_api.request(task_name, payload or {})
