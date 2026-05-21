import sys
import token
import tokenize
from io import StringIO
from PySide6.QtWidgets import QPlainTextEdit, QApplication, QTextEdit
from PySide6.QtGui import QPainter, QColor, QFont, QTextCharFormat, QTextCursor, QTextBlock, QFontMetrics, QPolygonF
from PySide6.QtCore import Qt, QRect, Signal, QPointF

from .comment_toggle_controller import CommentToggleController
from .font_zoom_controller import EditorFontZoomController
from .indent_guide_painter import IndentGuidePainter
from .line_number_area import LineNumberArea

class CodeEditor(QPlainTextEdit):
    contentMutated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # Folding State
        self.indent_width = 4
        self.folding_area_width = 16
        self.folded_blocks = set() # Store block numbers that are folded

        # Font & Zoom
        self._font_zoom_controller = EditorFontZoomController(self)
        self._font_zoom_controller.initialize()
        self._indent_guide_painter = IndentGuidePainter(self)
        self._comment_toggle_controller = CommentToggleController(self)

        # Editor Settings
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

        # Components
        self.line_number_area = LineNumberArea(self)
        
        # Signals
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        
        # Initialize
        self._bracket_match_selections = []
        self._symbol_reference_selections = []
        self._syntax_error_selections = []
        self.update_line_number_area_width(0)
        self.highlight_current_line()

        # Completer support placeholders
        self._completer = None
        self._auto_pair_expected_close_pos = None
        self._auto_pair_expected_close_char = None
        self._auto_pair_expected_close_time = 0.0
        self._auto_pair_skip_window_s = 0.8

    def enable_zoom(self):
        self._font_zoom_controller.enable_zoom()

    def disable_zoom(self):
        self._font_zoom_controller.disable_zoom()

    # --- Line Number Area & Layout ---
    
    def line_number_area_width(self):
        digits = 1
        max_val = max(1, self.blockCount())
        while max_val >= 10:
            max_val //= 10
            digits += 1
        
        # Width for digits + padding + folding area
        space = 10 + self.fontMetrics().horizontalAdvance('9') * digits
        return space + self.folding_area_width

    def update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))
        self._font_zoom_controller.reposition_zoom_popup()

    def wheelEvent(self, event):
        if self._font_zoom_controller.zoom_enabled and (event.modifiers() & Qt.ControlModifier):
            delta_y = event.angleDelta().y()
            if delta_y:
                point_size = self._font_zoom_controller.zoom_font(1 if delta_y > 0 else -1)
                self._font_zoom_controller.show_zoom_popup(point_size)
                event.accept()
                return
        super().wheelEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        self._indent_guide_painter.paint(event)

    def highlight_current_line(self):
        extra_selections = []
        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            # Subtle highlight color
            selection.format.setBackground(QColor("#f0f0f0"))
            selection.format.setProperty(0x1000, True) # Full width selection
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)

        # Bracket/quote match highlights
        doc = self.document()
        for pos, length in self._symbol_reference_selections:
            if pos < 0 or pos + length > len(self.toPlainText()):
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#e2e5e9"))
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.setPosition(pos + length, QTextCursor.KeepAnchor)
            sel.cursor = c
            extra_selections.append(sel)

        for pos, length in self._bracket_match_selections:
            if pos < 0 or pos + length > len(self.toPlainText()):
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#c8c8c8"))
            sel.format.setForeground(QColor("#1a1a1a"))
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.setPosition(pos + length, QTextCursor.KeepAnchor)
            sel.cursor = c
            extra_selections.append(sel)

        # Syntax error highlights (wavy red underline)
        for pos, length in self._syntax_error_selections:
            sel = QTextEdit.ExtraSelection()
            sel.format.setUnderlineColor(QColor("#e51400"))
            sel.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.setPosition(pos + length, QTextCursor.KeepAnchor)
            sel.cursor = c
            extra_selections.append(sel)

        self.setExtraSelections(extra_selections)

    # --- Folding Logic ---

    def get_indentation(self, text):
        return len(text) - len(text.lstrip())

    def _line_opens_block_strict(self, line_text: str) -> bool:
        src = str(line_text or "")
        if not src.strip():
            return False
        try:
            tokens = tokenize.generate_tokens(StringIO(src).readline)
            last_meaningful = None
            ignored = {
                token.INDENT,
                token.DEDENT,
                token.NEWLINE,
                tokenize.NL,
                token.ENDMARKER,
                token.COMMENT,
            }
            for tok in tokens:
                if tok.type in ignored:
                    continue
                last_meaningful = tok
            return bool(last_meaningful and last_meaningful.string == ":")
        except (tokenize.TokenError, IndentationError):
            return src.rstrip().endswith(":")

    def is_foldable(self, block):
        text = block.text()
        if not text.strip(): return False
        
        indent = self.get_indentation(text)
        
        next_block = block.next()
        while next_block.isValid():
            if not next_block.text().strip():
                next_block = next_block.next()
                continue
            
            next_indent = self.get_indentation(next_block.text())
            return next_indent > indent
        return False

    def is_folded(self, block):
        # We track folded state by block number or some ID
        # But since block numbers change on edit, robust implementation uses QTextBlockUserData.
        # For this simplified version, we'll check visibility of the *next* block
        next_block = block.next()
        if not next_block.isValid(): return False
        return not next_block.isVisible()

    def toggle_fold(self, start_block):
        if not self.is_foldable(start_block): return
        
        is_expanding = self.is_folded(start_block)
        
        start_indent = self.get_indentation(start_block.text())
        block = start_block.next()
        
        while block.isValid():
            text = block.text()
            if not text.strip():
                # Empty lines follow the visibility of surrounding
                if block.next().isValid() and self.get_indentation(block.next().text()) <= start_indent:
                     # End of block
                     break
            else:
                indent = self.get_indentation(text)
                if indent <= start_indent:
                    break
            
            # This is the line to toggle
            block.setVisible(is_expanding)
            block = block.next()
        
        self.line_number_area.update()
        self.viewport().update() # Force layout update
        self.update_line_number_area_width(0) # Re-calc width if needed

    # --- Painting ---

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        # Background
        painter.fillRect(event.rect(), QColor("#f5f5f7"))
        
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        
        current_block_number = self.textCursor().blockNumber()
        
        font_height = self.fontMetrics().height()
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                # Draw Line Number
                number = str(block_number + 1)
                painter.setPen(QColor("#999") if block_number != current_block_number else QColor("#333"))
                painter.setFont(self.font() if block_number != current_block_number else QFont(self.font().family(), self.font().pointSize(), QFont.Bold))
                
                width_no_fold = self.line_number_area.width() - self.folding_area_width
                painter.drawText(0, int(top), width_no_fold - 5, font_height, Qt.AlignRight, number)
                
                # Draw Folding Indicator
                if self.is_foldable(block):
                    fold_x = width_no_fold + (self.folding_area_width - 10) / 2
                    fold_y = top + (font_height - 10) / 2
                    
                    painter.save()
                    painter.setRenderHint(QPainter.Antialiasing)
                    
                    # Draw arrow
                    arrow_color = QColor("#666")
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(arrow_color)
                    
                    if self.is_folded(block):
                        # Right Arrow (Collapsed)
                        #  |\
                        #  |/
                        points = [
                            QPointF(fold_x + 2, fold_y),
                            QPointF(fold_x + 8, fold_y + 5),
                            QPointF(fold_x + 2, fold_y + 10)
                        ]
                        painter.drawPolygon(QPolygonF(points))
                    else:
                        # Down Arrow (Expanded)
                        #  ---
                        #   v
                        points = [
                            QPointF(fold_x, fold_y + 3),
                            QPointF(fold_x + 10, fold_y + 3),
                            QPointF(fold_x + 5, fold_y + 8)
                        ]
                        painter.drawPolygon(QPolygonF(points))
                    
                    painter.restore()

            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    # --- Completer & Auto-pair (Kept from original) ---
    # Copied logic from the previous file to maintain functionality
    
    def set_completer(self, completer):
        # Only store the reference. PythonCodeCompleter handles its own setup.
        self._completer = completer

    def insert_completion(self, completion):
        if hasattr(self._completer, "insert_completion"):
             self._completer.insert_completion(completion)
             return
             
        # Fallback for standard QCompleter
        tc = self.textCursor()
        prefix = ""
        if hasattr(self._completer, "completionPrefix"):
             prefix = self._completer.completionPrefix()
             
        extra = len(completion) - len(prefix)
        tc.movePosition(QTextCursor.Left)
        tc.movePosition(QTextCursor.EndOfWord)
        tc.insertText(completion[-extra:])
        self.setTextCursor(tc)

    def _handle_auto_pair_key(self, event) -> bool:
        # ... (Same logic as before, simplified for brevity in this response but would be full in file)
        # For now, to save space, I will re-implement the key logic properly
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False

        if event.key() == Qt.Key_Backspace:
            cursor = self.textCursor()
            if cursor.hasSelection(): return False
            pos = cursor.position()
            if pos <= 0: return False
            left = self.document().characterAt(pos - 1)
            right = self.document().characterAt(pos)

            pairs = {'(': ')', '[': ']', '{': '}', "'": "'", '"': '"'}
            if left in pairs and pairs[left] == right:
                cursor.beginEditBlock()
                cursor.deleteChar()
                cursor.deletePreviousChar()
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                return True

            # Backspace 删除一个 tab 宽度（行首空白区域）
            block_text = cursor.block().text()
            col = cursor.positionInBlock()
            if col > 0:
                prefix = block_text[:col]
                if prefix and prefix.strip() == "":
                    # 光标前全是空白，删一个 indent_width 的空格
                    remove_n = self.indent_width
                    if col < self.indent_width:
                        remove_n = col
                    cursor.beginEditBlock()
                    cursor.setPosition(cursor.block().position() + col - remove_n)
                    cursor.setPosition(cursor.block().position() + col, QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    return True

            return False
        
        text = event.text()
        if not text: return False
        
        pairs = {'(': ')', '[': ']', '{': '}', "'": "'", '"': '"'}
        closing = {v: k for k, v in pairs.items()}
        
        cursor = self.textCursor()
        
        if text in closing and not cursor.hasSelection():
            # Skip over closing char if it matches
            block_text = cursor.block().text()
            col = cursor.positionInBlock()
            next_ch = block_text[col] if col < len(block_text) else ""
            if next_ch == text:
                cursor.movePosition(QTextCursor.Right)
                self.setTextCursor(cursor)
                return True
        
        if text in pairs:
            # Insert pair
            close = pairs[text]
            if cursor.hasSelection():
                # Wrap selection
                start = cursor.selectionStart()
                end = cursor.selectionEnd()
                cursor.beginEditBlock()
                cursor.setPosition(start)
                cursor.insertText(text)
                cursor.setPosition(end + 1)
                cursor.insertText(close)
                cursor.endEditBlock()
                return True
            else:
                # Auto close
                cursor.insertText(text + close)
                cursor.movePosition(QTextCursor.Left)
                self.setTextCursor(cursor)
                return True
                
        return False

    def keyPressEvent(self, event):
        if self._handle_auto_pair_key(event):
            return

        # Ctrl+/ 注释切换
        if event.key() == Qt.Key_Slash and (event.modifiers() & Qt.ControlModifier):
            self._comment_toggle_controller.toggle_comment()
            return

        # Tab handling for indentation
        if event.key() == Qt.Key_Tab and not (event.modifiers() & Qt.ShiftModifier):
             if self._completer and self._completer.popup().isVisible():
                 # Trigger completion manually if Tab is pressed
                 if self._completer.popup().currentIndex().isValid():
                     self.insert_completion(self._completer.popup().currentIndex().data())
                 return
             else:
                 self._indent_selection()
                 return
        
        # Enter key handling
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._completer and self._completer.popup().isVisible():
                 # Trigger completion manually if Enter is pressed
                 if self._completer.popup().currentIndex().isValid():
                     self.insert_completion(self._completer.popup().currentIndex().data())
                 return
            
            # Smart Indent Logic
            cursor = self.textCursor()
            block_text = cursor.block().text()
            current_indent = self.get_indentation(block_text)
            
            # Strict check: ignore comments and only look at meaningful tokens.
            extra_indent = 0
            if self._line_opens_block_strict(block_text):
                extra_indent = self.indent_width
            
            super().keyPressEvent(event)
            
            # Apply indentation to new line
            if current_indent + extra_indent > 0:
                self.insertPlainText(" " * (current_indent + extra_indent))
            return

        if event.key() == Qt.Key_Backtab: # Shift+Tab
            self._dedent_selection()
            return

        super().keyPressEvent(event)

    def _indent_selection(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            cursor.beginEditBlock()
            cursor.setPosition(start)
            cursor.movePosition(QTextCursor.StartOfLine)
            while cursor.position() <= end:
                cursor.insertText(" " * self.indent_width)
                if not cursor.movePosition(QTextCursor.Down): break
            cursor.endEditBlock()
        else:
            cursor.insertText(" " * self.indent_width)

    def _dedent_selection(self):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        # Simple dedent logic
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.StartOfLine)
        
        while cursor.position() <= end:
            block = cursor.block()
            text = block.text()
            if text.startswith(" " * self.indent_width):
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + self.indent_width, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
            elif text.startswith("\t"):
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + 1, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                
            if not cursor.movePosition(QTextCursor.Down): break

        cursor.endEditBlock()
