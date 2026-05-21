from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel


class EditorFontZoomController:
    def __init__(self, editor):
        self.editor = editor
        self._min_font_point_size = 8
        self._max_font_point_size = 24
        self._default_font_point_size = 10
        self._zoom_enabled = True
        self._zoom_popup = None
        self._zoom_popup_timer = None

    @property
    def zoom_enabled(self):
        return self._zoom_enabled

    def initialize(self):
        font = QFont("Consolas")
        if not font.exactMatch():
            font = QFont("Courier New")
        font.setPointSize(self._default_font_point_size)
        self.editor.setFont(font)
        metrics = self.editor.fontMetrics()
        self.editor.setTabStopDistance(metrics.horizontalAdvance(" ") * self.editor.indent_width)

        self._zoom_popup = QLabel(self.editor.viewport())
        self._zoom_popup.setObjectName("EditorZoomPopup")
        self._zoom_popup.setAlignment(Qt.AlignCenter)
        self._zoom_popup.setFixedSize(88, 44)
        self._zoom_popup.setStyleSheet(
            """
            QLabel#EditorZoomPopup {
                background: rgba(32, 36, 42, 220);
                color: #ffffff;
                border-radius: 10px;
                font-size: 16px;
                font-weight: 600;
                padding: 4px 8px;
            }
            """
        )
        self._zoom_popup.hide()

        self._zoom_popup_timer = QTimer(self.editor)
        self._zoom_popup_timer.setSingleShot(True)
        self._zoom_popup_timer.setInterval(2000)
        self._zoom_popup_timer.timeout.connect(self._zoom_popup.hide)

    def apply_editor_font(self, point_size: int):
        point_size = max(self._min_font_point_size, min(self._max_font_point_size, int(point_size)))
        font = self.editor.font()
        if font.pointSize() == point_size:
            return point_size

        font.setPointSize(point_size)
        self.editor.setFont(font)
        metrics = self.editor.fontMetrics()
        self.editor.setTabStopDistance(metrics.horizontalAdvance(" ") * self.editor.indent_width)
        self.editor.update_line_number_area_width(0)

        line_number_area = getattr(self.editor, "line_number_area", None)
        if line_number_area is not None:
            line_number_area.update()
        self.editor.viewport().update()
        self.reposition_zoom_popup()
        return point_size

    def zoom_font(self, delta_steps: int):
        if not delta_steps:
            return self.editor.font().pointSize()

        current_size = self.editor.font().pointSize()
        if current_size <= 0:
            current_size = self._default_font_point_size
        return self.apply_editor_font(current_size + delta_steps)

    def reposition_zoom_popup(self):
        if self._zoom_popup is None:
            return
        x = max((self.editor.viewport().width() - self._zoom_popup.width()) // 2, 0)
        y = max((self.editor.viewport().height() - self._zoom_popup.height()) // 2, 0)
        self._zoom_popup.move(x, y)

    def show_zoom_popup(self, point_size: int):
        if not self._zoom_enabled or self._zoom_popup is None or self._zoom_popup_timer is None:
            return
        self._zoom_popup.setText(str(point_size))
        self.reposition_zoom_popup()
        self._zoom_popup.show()
        self._zoom_popup.raise_()
        self._zoom_popup_timer.start()

    def enable_zoom(self):
        self._zoom_enabled = True

    def disable_zoom(self):
        self._zoom_enabled = False
        if self._zoom_popup_timer is not None:
            self._zoom_popup_timer.stop()
        if self._zoom_popup is not None:
            self._zoom_popup.hide()
