from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen


class IndentGuidePainter:
    def __init__(self, editor):
        self.editor = editor

    def paint(self, event):
        space_width = self.editor.fontMetrics().horizontalAdvance(" ")
        indent_step_px = max(space_width * self.editor.indent_width, 1)
        painter = QPainter(self.editor.viewport())
        pen = QPen(QColor(160, 166, 176, 38))
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)

        block = self.editor.firstVisibleBlock()
        offset = self.editor.contentOffset()
        top = self.editor.blockBoundingGeometry(block).translated(offset).top()
        bottom = top + self.editor.blockBoundingRect(block).height()
        visible_rect = event.rect()

        while block.isValid() and top <= visible_rect.bottom():
            if block.isVisible() and bottom >= visible_rect.top():
                text = block.text()
                if text.strip():
                    indent_chars = len(text) - len(text.lstrip(" \t"))
                    indent_levels = max(indent_chars // self.editor.indent_width, 0)
                    for level in range(1, indent_levels + 1):
                        x = int(offset.x() + indent_step_px * level)
                        if 0 <= x <= self.editor.viewport().width():
                            painter.drawLine(x, int(top), x, int(bottom))

            block = block.next()
            top = bottom
            bottom = top + self.editor.blockBoundingRect(block).height()
