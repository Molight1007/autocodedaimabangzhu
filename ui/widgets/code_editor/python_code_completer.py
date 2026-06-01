"""Python 代码补全器。

核心职责：
- 为 CodeEditor 提供实时代码补全功能，包括关键字、内置函数、模块属性。
- 管理补全弹窗的生命周期（显示、隐藏、接受、过滤）。
- 通过使用频率排序优化候选词顺序。

不负责：
- 不执行代码或验证语法正确性。
- 不管理编辑器本身的文本编辑逻辑（由 CodeEditor 处理）。
- 不持久化使用频率数据（内存级缓存，随实例销毁而丢失）。

数据流：
用户输入 → _on_text_changed 信号 → _trigger_completion → 
Python 端过滤候选词 → 更新 QStringListModel → QCompleter 显示弹窗 →
用户选择 → _insert_completion 插入文本。

关键组件协作：
- CompleterListView: 自定义 QListView，提供可靠的可见性检测。
- completion_parentheses: 提供模块属性元数据和括号插入判断。
- python_keywords.json: 静态关键字、内置函数、标准库模块的数据源。

约束：
- 必须与 CodeEditor 实例一对一绑定。
- 依赖编辑器安装 eventFilter 拦截 Tab/Enter/Escape 键。
- 异步初始化数据（100ms 延迟），避免启动时阻塞 UI。
"""

from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QStringListModel
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QListView

from ...utils.python_keywords import (
    BUILTINS,
    CALLABLE_BUILTINS,
    KEYWORDS,
    STDLIB_MODULES,
)
from .completion_parentheses import (
    DEFAULT_ATTR_MODULES,
    collect_module_attr_metadata,
    should_insert_call_parentheses,
)
from .type_inferrer import TypeInferrer
from .type_attr_cache import TypeAttrCache

DEBUG_COMPLETER_TRACE = False


class CompleterListView(QListView):
    """自定义补全列表视图。

    解决 QCompleter 默认 popup 在特定场景下 isVisible() 状态不准确的问题。
    通过内部状态标记 _is_visible 与 Qt 实际可见状态联合判断，提供更可靠的可见性检测。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_visible = False
        if DEBUG_COMPLETER_TRACE:
            print(f"TRACE: CompleterListView created. ID={id(self)}")

    def showEvent(self, event):
        self._is_visible = True
        if DEBUG_COMPLETER_TRACE:
            print(f"TRACE: CompleterListView showEvent. ID={id(self)}")
        super().showEvent(event)

    def hideEvent(self, event):
        self._is_visible = False
        if DEBUG_COMPLETER_TRACE:
            print(f"TRACE: CompleterListView hideEvent. ID={id(self)}")
        super().hideEvent(event)

    def is_visible_safe(self):
        real_visible = self.isVisible()
        return self._is_visible or real_visible


class PythonCodeCompleter(QObject):
    """Python 代码补全器主类。

    生命周期约束：
    - 必须与 editor 一对一绑定，且 parent 应为 editor 的父级或独立 QObject。
    - 将 QCompleter 的 parent 设为 self 而非 editor，防止 editor 先于 completer 销毁时
      触发 0xC0000409 崩溃（底层指针悬空）。

    状态说明：
    - _static_words: 从 JSON 加载的静态词库，生命周期与实例相同。
    - _all_words: 动态词库（静态词 + 代码中提取的标识符），每 300ms 重建一次。
    - _usage_counts: 使用频率计数，内存级缓存，不持久化。
    - _attr_sources: 已加载模块的属性缓存，按需延迟加载。
    """

    def __init__(self, editor: QPlainTextEdit, parent=None):
        super().__init__(parent)
        self._editor = editor

        self._model = QStringListModel(self)

        # parent 设为 self 而非 editor，避免销毁顺序导致的崩溃
        self._completer = QCompleter(self._model, self)

        self._popup_view = CompleterListView()
        self._completer.setPopup(self._popup_view)

        self._completer.setWidget(self._editor)
        self._editor.removeEventFilter(self._completer)

        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        # UnfilteredPopupCompletion: 我们手动过滤，QCompleter 只做展示
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.activated[str].connect(self._insert_completion)

        self._keywords: Set[str] = set()
        self._builtins: Set[str] = set()
        self._static_words: Set[str] = set()
        self._all_words: List[str] = []
        self._last_base_source = None

        self._usage_counts: Dict[str, int] = {}
        self._attr_sources: Dict[str, List[str]] = {}
        self._callable_attr_sources: Dict[str, Set[str]] = {}
        self._loaded_attr_modules: Set[str] = set()
        self._imported_modules: Set[str] = set()
        self._stdlib_modules: List[str] = self._build_stdlib_modules()

        self._type_inferrer = TypeInferrer()
        self._type_attr_cache = TypeAttrCache()

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._rebuild_dynamic_words)

        self._trigger_timer = QTimer(self)
        self._trigger_timer.setSingleShot(True)
        self._trigger_timer.setInterval(10)
        self._trigger_timer.timeout.connect(self._trigger_completion)

        self._editor.installEventFilter(self)
        self._editor.textChanged.connect(self._on_text_changed)

        # 异步初始化：避免启动时阻塞 UI，同时防止对象销毁后回调触发崩溃
        self._init_timer = QTimer(self)
        self._init_timer.setSingleShot(True)
        self._init_timer.timeout.connect(self._init_data)
        self._init_timer.start(100)
        self._editor.setProperty("_python_code_completer", self)
        self._editor.setProperty("_python_code_completer_popup", self._popup_view)
        self._typo_fixes = {
            "imprt": "import",
            "imoprt": "import",
        }

    def popup(self):
        return self._popup_view

    def widget(self):
        return self._editor

    def completionPrefix(self):
        return self._completer.completionPrefix()

    def insert_completion(self, text: str):
        self._insert_completion(text)

    def is_popup_visible(self):
        return self._popup_view.is_visible_safe()

    def _init_data(self):
        try:
            if not self._editor or not self._editor.isVisible():
                return

            self._static_words = self._build_static_words()
            self._all_words = sorted(self._static_words)
            self._attr_sources = self._build_attr_sources()
        except Exception:
            pass

    def _on_text_changed(self):
        self._trigger_timer.start()
        self._rebuild_timer.start(300)

    def _compute_ordered_candidates_for_cursor(self) -> Tuple[str, str, List[str]]:
        try:
            cursor = self._editor.textCursor()
            block_text = cursor.block().text()
            col = cursor.positionInBlock()
            prev_char = block_text[col - 1] if col > 0 and col <= len(block_text) else ""

            obj_name, attr_prefix = self._current_attr_context()
            if obj_name and obj_name in self._attr_sources:
                base = self._attr_sources[obj_name]
                prefix = attr_prefix
            else:
                base = self._all_words
                prefix = self._current_prefix()

                if not prefix and prev_char != ".":
                    return "", prev_char, []

            if prefix:
                prefix_lower = prefix.lower()
                candidates = [w for w in base if w.lower().startswith(prefix_lower)]
            else:
                candidates = list(base)

            if not candidates:
                return prefix, prev_char, []

            ordered = self._sort_candidates(candidates)
            return prefix, prev_char, ordered
        except Exception:
            return "", "", []

    def accept_current_completion_for_tab(self) -> bool:
        index = self._popup_view.currentIndex()
        chosen = index.data() if index.isValid() else None
        if not chosen and self._model.rowCount() > 0:
            chosen = self._model.index(0, 0).data()
        if chosen:
            self._insert_completion(chosen)
        self._popup_view.hide()
        return True

    def accept_current_completion_for_enter(self) -> bool:
        return self.accept_current_completion_for_tab()

    def eventFilter(self, obj, event):
        if obj is self._editor and event.type() == QEvent.KeyPress:
            key = event.key()

            if self._popup_view.is_visible_safe():
                if key == Qt.Key_Tab and not (event.modifiers() & Qt.ShiftModifier):
                    if DEBUG_COMPLETER_TRACE:
                        print(f"TRACE: Popup visible, Tab pressed. Key={key}")
                    return self.accept_current_completion_for_tab()

                if key in (Qt.Key_Enter, Qt.Key_Return):
                    if DEBUG_COMPLETER_TRACE:
                        print(f"TRACE: Popup visible, Enter pressed. Key={key}")
                    return self.accept_current_completion_for_enter()

                if key == Qt.Key_Escape:
                    self._popup_view.hide()
                    return True

                return False

            # 手动触发：Ctrl+Space
            if key == Qt.Key_Space and (event.modifiers() & Qt.ControlModifier):
                if DEBUG_COMPLETER_TRACE:
                    print("TRACE: Ctrl+Space detected")
                self._trigger_completion()
                return True

            return False

        return super().eventFilter(obj, event)

    def _build_static_words(self) -> Set[str]:
        self._keywords = set(KEYWORDS)
        self._builtins = set(BUILTINS)
        self._callable_builtins = CALLABLE_BUILTINS
        return self._keywords | self._builtins

    def _build_attr_sources(self) -> Dict[str, List[str]]:
        sources, callable_sources = collect_module_attr_metadata(DEFAULT_ATTR_MODULES)
        self._callable_attr_sources = dict(callable_sources)
        self._loaded_attr_modules = set(DEFAULT_ATTR_MODULES)
        return sources

    def _refresh_dynamic_attr_sources(self, modules: Set[str]) -> None:
        pending: List[str] = []
        for mod in set(modules or set()):
            name = str(mod or "").strip()
            if not name:
                continue
            if name in self._loaded_attr_modules:
                continue
            pending.append(name)
        if not pending:
            return

        sources, callable_sources = collect_module_attr_metadata(pending)
        for name in pending:
            self._loaded_attr_modules.add(name)
        for name, attrs in sources.items():
            self._attr_sources[name] = list(attrs)
        for name, attrs in callable_sources.items():
            self._callable_attr_sources[name] = set(attrs)

    def _build_stdlib_modules(self) -> List[str]:
        try:
            import sys

            names = getattr(sys, "stdlib_module_names", None)
            if names:
                return sorted({n for n in names if isinstance(n, str) and n and "." not in n})
        except Exception:
            pass

        return sorted(STDLIB_MODULES)

    def _rebuild_dynamic_words(self):
        text = self._editor.toPlainText()
        tokens = set(re.findall(r"\b[A-Za-z_]\w*\b", text))
        words = list(self._static_words | tokens)
        words.sort()
        self._all_words = words
        self._imported_modules = self._extract_imported_modules(text)
        self._refresh_dynamic_attr_sources(self._imported_modules)
        # 更新变量类型推断（供 obj.attr 补全使用）
        self._type_inferrer.analyze(text)

    def _extract_imported_modules(self, text: str) -> Set[str]:
        imported: Set[str] = set()
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue

            if s.startswith("import "):
                rest = s[7:].split("#", 1)[0].strip()
                for item in rest.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if " as " in item:
                        mod = item.split(" as ", 1)[0].strip()
                    else:
                        mod = item
                    if not mod:
                        continue
                    imported.add(mod)
                    imported.add(mod.split(".", 1)[0])
                continue

            if s.startswith("from "):
                rest = s[5:].split("#", 1)[0]
                if " import " in rest:
                    mod = rest.split(" import ", 1)[0].strip()
                else:
                    mod = rest.strip()
                if mod:
                    imported.add(mod)
                    imported.add(mod.split(".", 1)[0])
                continue

        return imported

    def _current_import_context(self) -> Tuple[str, str]:
        cursor = self._editor.textCursor()
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        left = block_text[:col]
        s = left.lstrip()

        if s.startswith("import "):
            rest = s[7:].split("#", 1)[0]
            last = rest.split(",")[-1].strip()
            if " as " in last:
                last = last.split(" as ", 1)[0].strip()
            return "import", last

        if s.startswith("from "):
            rest = s[5:].split("#", 1)[0]
            if " import " in rest:
                mod = rest.split(" import ", 1)[0].strip()
            else:
                mod = rest.strip()
            return "from", mod

        return "", ""

    def _current_prefix(self) -> str:
        cursor = self._editor.textCursor()
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        i = col - 1
        while i >= 0:
            ch = block_text[i]
            if not (ch.isalnum() or ch == "_"):
                break
            i -= 1
        prefix = block_text[i + 1 : col]
        return prefix

    def _current_attr_context(self) -> Tuple[str, str]:
        cursor = self._editor.textCursor()
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        left = block_text[:col]
        m = re.search(r"([A-Za-z_]\w*)\.(\w*)$", left)
        if not m:
            return "", ""
        return m.group(1), m.group(2)

    def _sort_candidates(self, words: List[str]) -> List[str]:
        def sort_key(w: str):
            usage = self._usage_counts.get(w, 0)
            is_keyword = 1 if w in self._keywords else 0
            is_builtin = 1 if w in self._builtins else 0
            return -usage, -is_keyword, -is_builtin, w.lower()

        return sorted(words, key=sort_key)

    def _is_inside_string(self) -> bool:
        """检测光标是否位于字符串字面量内部（避免字符串内弹出补全）。"""
        cursor = self._editor.textCursor()
        block = cursor.block()
        col = cursor.positionInBlock()
        text_before = block.text()[:col]

        # 上一行处于三引号字符串内部（依赖 syntax highlighter 设置的 block state）
        prev_block = block.previous()
        if prev_block.isValid() and prev_block.userState() in (1, 2):
            return True

        in_single = False
        in_double = False
        i = 0
        while i < len(text_before):
            ch = text_before[i]

            # 三引号优先匹配
            if text_before[i:i + 3] == '"""' and not in_single:
                in_double = not in_double
                i += 3
                continue
            if text_before[i:i + 3] == "'''" and not in_double:
                in_single = not in_single
                i += 3
                continue

            # 转义字符跳过
            if ch == '\\':
                i += 2
                continue

            if ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "'" and not in_double:
                in_single = not in_single

            i += 1

        return in_single or in_double

    def _trigger_completion(self):
        """触发代码补全的核心逻辑。

        采用"Python 主动过滤 + 全量刷新 Model"的策略，比依赖 QCompleter 内部过滤更稳定，
        能有效避免 0xC0000409 崩溃。
        """
        try:
            if not self._editor.isVisible():
                return
        except RuntimeError:
            return

        # 字符串内部不触发补全
        if self._is_inside_string():
            self._completer.popup().hide()
            return

        cursor = self._editor.textCursor()
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        prev_char = block_text[col - 1] if 0 < col <= len(block_text) else ""

        import_kind, import_prefix = self._current_import_context()

        obj_name, attr_prefix = self._current_attr_context()

        if import_kind:
            base = self._stdlib_modules
            prefix = import_prefix
            if not prefix:
                self._completer.popup().hide()
                return
        elif obj_name and obj_name in self._attr_sources:
            base = self._attr_sources[obj_name]
            prefix = attr_prefix
        elif obj_name:
            # obj.attr 上下文：obj 不在已知模块中 → 尝试类型推断
            inferred_type = self._type_inferrer.get_type(obj_name)
            if inferred_type:
                base = self._type_attr_cache.get_attrs(inferred_type)
                prefix = attr_prefix
            else:
                base = self._all_words
                prefix = self._current_prefix()
                if not prefix and prev_char != ".":
                    self._completer.popup().hide()
                    return
        else:
            base = self._all_words
            prefix = self._current_prefix()

            if not prefix and prev_char != ".":
                self._completer.popup().hide()
                return

        if prefix:
            prefix_lower = prefix.lower()
            candidates = [w for w in base if w.lower().startswith(prefix_lower)]
        else:
            candidates = list(base)

        if not import_kind and candidates:
            imported_modules = self._imported_modules
            stdlib_modules = set(self._stdlib_modules)
            candidates = [w for w in candidates if (w not in stdlib_modules or w in imported_modules)]

        if not candidates and prefix:
            fixed = self._typo_fixes.get(prefix.lower())
            if fixed:
                candidates = [fixed]

        if DEBUG_COMPLETER_TRACE:
            print(f"TRACE: Prefix='{prefix}', Candidates found={len(candidates)}")

        if not candidates:
            self._completer.popup().hide()
            return

        ordered = self._sort_candidates(candidates)

        self._model.setStringList(ordered)

        self._completer.setCompletionPrefix(prefix)

        rect = self._editor.cursorRect()
        fm = self._editor.fontMetrics()
        rect.translate(0, fm.height())

        popup = self._completer.popup()
        target_width = popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width()
        rect.setWidth(max(target_width, 200))

        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        self._completer.complete(rect)

        if not popup.isVisible():
            popup.show()

    def _insert_completion(self, text: str):
        self._trigger_timer.stop()
        self._editor.blockSignals(True)
        try:
            cursor = self._editor.textCursor()
            attr_owner, _ = self._current_attr_context()
            block_text = cursor.block().text()
            col = cursor.positionInBlock()
            i = col - 1
            while i >= 0:
                ch = block_text[i]
                if not (ch.isalnum() or ch == "_"):
                    break
                i -= 1
            cursor.beginEditBlock()
            cursor.setPosition(cursor.block().position() + i + 1)
            cursor.setPosition(cursor.block().position() + col, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(text)
            if text in {"import", "from", "def", "class", "return", "raise"}:
                block_text2 = cursor.block().text()
                col2 = cursor.positionInBlock()
                next_ch = block_text2[col2] if col2 < len(block_text2) else ""
                if next_ch == "" or next_ch.isalnum() or next_ch == "_":
                    cursor.insertText(" ")
            else:
                block_text2 = cursor.block().text()
                col2 = cursor.positionInBlock()
                next_ch = block_text2[col2] if col2 < len(block_text2) else ""
                should_paren = should_insert_call_parentheses(
                    completion=text,
                    next_char=next_ch,
                    attr_owner=attr_owner,
                    callable_builtins=self._callable_builtins,
                    callable_attr_sources=self._callable_attr_sources,
                )
                # 类型推断扩展：属性补全时检查推断类型的可调用性
                if not should_paren and attr_owner:
                    inferred_type = self._type_inferrer.get_type(attr_owner)
                    if inferred_type and text in self._type_attr_cache.get_callables(inferred_type):
                        should_paren = True
                if should_paren:
                    cursor.insertText("()")
                    cursor.movePosition(QTextCursor.Left)
            cursor.endEditBlock()
            self._editor.setTextCursor(cursor)
            self._usage_counts[text] = self._usage_counts.get(text, 0) + 1
        finally:
            self._editor.blockSignals(False)
            self._completer.popup().hide()
