from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QWidget


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            x = event.pos().x()
            if x > self.width() - self.editor.folding_area_width:
                y = event.pos().y()
                block = self.editor.firstVisibleBlock()
                top = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset()).top()
                bottom = top + self.editor.blockBoundingRect(block).height()

                while block.isValid():
                    if top <= y <= bottom:
                        if self.editor.is_foldable(block):
                            self.editor.toggle_fold(block)
                            return

                    block = block.next()
                    top = bottom
                    bottom = top + self.editor.blockBoundingRect(block).height()
                    if top > self.height():
                        break
        super().mousePressEvent(event)
