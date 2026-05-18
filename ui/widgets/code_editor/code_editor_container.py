from typing import Any, Optional, Dict

from PySide6.QtWidgets import QWidget, QVBoxLayout

from .code_editor import CodeEditor
from .python_syntax_highlighter import PythonSyntaxHighlighter
from .python_code_completer import PythonCodeCompleter
from .bracket_match_highlighter import BracketMatchHighlighter


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
        self._dispatch_api = None

        self._highlighter = PythonSyntaxHighlighter(self.editor.document())
        self._completer = PythonCodeCompleter(self.editor, self)
        self._bracket_matcher = BracketMatchHighlighter(self.editor, self)

        # 关键：将补全器引用传递给编辑器
        self.editor._completer = self._completer  # 直接设置属性

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
