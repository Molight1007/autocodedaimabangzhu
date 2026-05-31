from PySide6.QtWidgets import QStackedWidget


class AutoSizingStackedWidget(QStackedWidget):
    """让堆叠页高度跟随当前页，避免切换后保留上一页的大块留白。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(self._refresh_geometry)

    def sizeHint(self):
        current = self.currentWidget()
        if current is not None:
            return current.sizeHint()
        return super().sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        if current is not None:
            return current.minimumSizeHint()
        return super().minimumSizeHint()

    def _refresh_geometry(self, _index):
        self.updateGeometry()
        current = self.currentWidget()
        if current is not None:
            current.updateGeometry()
