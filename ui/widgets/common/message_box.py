from __future__ import annotations

from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import Qt, QSize, QPoint
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QTextBrowser,
    QApplication,
    QGraphicsDropShadowEffect,
)

from ...styles.message_box_style import MESSAGE_BOX_QSS


class MessageType(Enum):
    """消息类型枚举"""
    INFORMATION = auto()
    WARNING = auto()
    ERROR = auto()
    SUCCESS = auto()
    QUESTION = auto()


class MessageBox(QDialog):
    """
    美观的自定义消息弹窗，替代原生 QMessageBox。

    支持类型：信息、警告、错误、成功、确认
    支持 HTML 内容显示
    """

    _ICONS = {
        MessageType.INFORMATION: ("ℹ", "#007AFF"),
        MessageType.WARNING: ("⚠", "#FF9500"),
        MessageType.ERROR: ("✕", "#FF3B30"),
        MessageType.SUCCESS: ("✓", "#34C759"),
        MessageType.QUESTION: ("?", "#007AFF"),
    }

    def __init__(
        self,
        parent=None,  # 父窗口，弹窗会显示在其中心位置
        title: str = "提示",  # 弹窗标题
        text: str = "",  # 弹窗内容（支持 HTML）
        msg_type: MessageType = MessageType.INFORMATION,  # 消息类型：信息/警告/错误/成功/确认
        buttons: Optional[list[str]] = None,  # 按钮文字列表，如["确定", "取消"]
        default_button: Optional[str] = None,  # 默认高亮的按钮文字
    ):
        super().__init__(parent)
        self._msg_type = msg_type
        self._result_button: Optional[str] = None
        self._drag_pos: Optional[QPoint] = None

        self._setup_window()
        self._setup_ui(title, text, buttons, default_button)
        self._apply_shadow()

    def _setup_window(self):
        """配置窗口属性"""
        self.setWindowFlags(
            Qt.Dialog
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(MESSAGE_BOX_QSS)
        # 默认宽高为屏幕的 40%
        screen = QApplication.primaryScreen().geometry()
        self.setFixedWidth(int(screen.width() * 0.4))
        self.setFixedHeight(int(screen.height() * 0.4))

    def _setup_ui(
        self,
        title: str,
        text: str,
        buttons: Optional[list[str]],
        default_button: Optional[str],
    ):
        """设置 UI 布局"""
        # 主容器
        container = QWidget(self)
        container.setObjectName("Container")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(container)

        content_layout = QVBoxLayout(container)
        content_layout.setContentsMargins(24, 24, 24, 20)
        content_layout.setSpacing(16)

        # 顶部：标题
        header = QHBoxLayout()
        header.setSpacing(12)
        header.setAlignment(Qt.AlignLeft)

        # 标题
        title_label = QLabel(title)
        title_label.setObjectName("TitleLabel")
        header.addWidget(title_label, 1)
        header.addStretch()

        content_layout.addLayout(header)

        # 内容区域
        content_browser = QTextBrowser()
        content_browser.setObjectName("ContentBrowser")
        content_browser.setOpenExternalLinks(True)
        content_browser.setHtml(self._format_content(text))
        content_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content_browser.setMinimumHeight(60)
        content_browser.setMaximumHeight(300)
        content_browser.setLineWrapMode(QTextBrowser.WidgetWidth)
        content_layout.addWidget(content_browser)

        # 按钮区域
        if buttons:
            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(10)
            btn_layout.addStretch()

            for btn_text in buttons:
                btn = QPushButton(btn_text)
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda checked, t=btn_text: self._on_button_clicked(t))

                # 设置默认按钮样式
                if btn_text == default_button:
                    btn.setObjectName(self._get_default_button_style())
                    btn.setDefault(True)

                btn_layout.addWidget(btn)

            content_layout.addLayout(btn_layout)

    def _create_icon(self) -> QLabel:
        """创建图标标签"""
        icon_char, color = self._ICONS.get(self._msg_type, ("ℹ", "#007AFF"))

        label = QLabel()
        label.setObjectName("IconLabel")
        label.setFixedSize(40, 40)

        # 绘制圆形背景图标
        pixmap = QPixmap(40, 40)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # 背景圆
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, 40, 40)

        # 图标文字
        painter.setPen(Qt.white)
        font = QFont("Segoe UI", 14, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, icon_char)
        painter.end()

        label.setPixmap(pixmap)
        return label

    def _format_content(self, text: str) -> str:
        """格式化内容为 HTML"""
        # 如果内容已经是 HTML，直接返回
        if text.strip().startswith(("<html", "<!DOCTYPE", "<p", "<div", "<span", "<b", "<i")):
            return text

        # 简单文本转换为 HTML
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        escaped = escaped.replace("\n", "<br>")
        return f"<html><body style='line-height:1.6;'>{escaped}</body></html>"

    def _get_default_button_style(self) -> str:
        """获取默认按钮样式名称"""
        style_map = {
            MessageType.INFORMATION: "PrimaryBtn",
            MessageType.WARNING: "WarningBtn",
            MessageType.ERROR: "DangerBtn",
            MessageType.SUCCESS: "SuccessBtn",
            MessageType.QUESTION: "PrimaryBtn",
        }
        return style_map.get(self._msg_type, "PrimaryBtn")

    def _on_button_clicked(self, button_text: str):
        """按钮点击处理"""
        self._result_button = button_text
        self.accept()

    def _apply_shadow(self):
        """应用阴影效果"""
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 60))
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)

    def mousePressEvent(self, event):
        """鼠标按下事件，记录拖动起始位置"""
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动事件，实现窗口拖动"""
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        """鼠标释放事件，清除拖动状态"""
        if event.button() == Qt.LeftButton:
            self._drag_pos = None
            event.accept()

    def get_clicked_button(self) -> Optional[str]:
        """获取用户点击的按钮文本"""
        return self._result_button

    @classmethod
    def information(
        cls,
        parent=None,
        title: str = "提示",
        text: str = "",
        button: str = "确定",
    ) -> str:
        """显示信息弹窗"""
        dlg = cls(
            parent=parent,
            title=title,
            text=text,
            msg_type=MessageType.INFORMATION,
            buttons=[button],
            default_button=button,
        )
        dlg.exec()
        return button

    @classmethod
    def warning(
        cls,
        parent=None,
        title: str = "警告",
        text: str = "",
        button: str = "确定",
    ) -> str:
        """显示警告弹窗"""
        dlg = cls(
            parent=parent,
            title=title,
            text=text,
            msg_type=MessageType.WARNING,
            buttons=[button],
            default_button=button,
        )
        dlg.exec()
        return button

    @classmethod
    def error(
        cls,
        parent=None,
        title: str = "错误",
        text: str = "",
        button: str = "确定",
    ) -> str:
        """显示错误弹窗"""
        dlg = cls(
            parent=parent,
            title=title,
            text=text,
            msg_type=MessageType.ERROR,
            buttons=[button],
            default_button=button,
        )
        dlg.exec()
        return button

    @classmethod
    def success(
        cls,
        parent=None,
        title: str = "成功",
        text: str = "",
        button: str = "确定",
    ) -> str:
        """显示成功弹窗"""
        dlg = cls(
            parent=parent,
            title=title,
            text=text,
            msg_type=MessageType.SUCCESS,
            buttons=[button],
            default_button=button,
        )
        dlg.exec()
        return button

    @classmethod
    def question(
        cls,
        parent=None,
        title: str = "确认",
        text: str = "",
        buttons: list[str] = None,
        default_button: str = None,
    ) -> Optional[str]:
        """
        显示确认弹窗

        Returns:
            用户点击的按钮文本，如果关闭弹窗则返回 None
        """
        if buttons is None:
            buttons = ["确定", "取消"]
        if default_button is None:
            default_button = buttons[0]

        dlg = cls(
            parent=parent,
            title=title,
            text=text,
            msg_type=MessageType.QUESTION,
            buttons=buttons,
            default_button=default_button,
        )
        result = dlg.exec()
        if result == QDialog.Accepted:
            return dlg.get_clicked_button()
        return None
