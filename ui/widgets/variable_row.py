from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QStackedWidget, QFrame, QToolButton
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QFont, QIcon
from core.models import VariableConfig
from ..styles.variable_row_style import VARIABLE_ROW_QSS
from .code_editor_dialog import CodeEditorDialog
from .common.clickable_line_edit import ClickableLineEdit

class VariableRowWidget(QFrame):
    """
    原子化变量配置行组件 (Modern Style)。
    """
    # 信号：请求删除自身
    delete_requested = Signal(QWidget)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VariableRow")
        self._code_text = ""
        self._code_template = "generate_data"
        
        # 汉化映射字典
        self.type_map = {"整数": "Integer", "浮点数": "Float", "字符": "String"}
        self.source_map = {"数据范围": "Range", "列表选择": "List", "字符集合": "Charset", "来自代码": "Code"}
        self.separator_map = {"无": "", "空格": " ", "换行": "\n", "制表符": "\t", "逗号": ",", "分号": ";"}
        
        self.setup_ui()
        self.setup_style()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(8)
        
        # 字体样式
        self.bold_font = QFont()
        self.bold_font.setBold(True)

        self.header_widget = QWidget()
        self.header_widget.setObjectName("HeaderWidget")
        self.header_layout = QHBoxLayout(self.header_widget)
        self.header_layout.setContentsMargins(6, 2, 6, 2)
        self.header_layout.setSpacing(10)

        self.expand_btn = QToolButton()
        self.expand_btn.setCheckable(True)
        self.expand_btn.setChecked(True)
        self.expand_btn.setArrowType(Qt.DownArrow)
        self.expand_btn.setCursor(Qt.PointingHandCursor)
        self.expand_btn.setFixedSize(20, 20)
        self.expand_btn.toggled.connect(self._set_expanded)
        self.header_layout.addWidget(self.expand_btn)

        self.title_label = QLabel("未命名变量")
        self.title_label.setObjectName("VarTitle")
        self.header_layout.addWidget(self.title_label)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("VarSummary")
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.header_layout.addWidget(self.summary_label, 1)
        
        # 删除按钮 (x)
        self.delete_btn = QPushButton("×") # 使用乘号或 x
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.setObjectName("DeleteBtn")
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        self.header_layout.addWidget(self.delete_btn)

        self.main_layout.addWidget(self.header_widget)

        self.details_container = QWidget()
        self.details_layout = QVBoxLayout(self.details_container)
        self.details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_layout.setSpacing(6)

        self.top_row_widget = QWidget()
        self.top_row_layout = QHBoxLayout(self.top_row_widget)
        self.top_row_layout.setContentsMargins(0, 0, 0, 0)
        self.top_row_layout.setSpacing(8)
        self.top_row_layout.setAlignment(Qt.AlignVCenter)

        lbl_name = QLabel("变量")
        lbl_name.setFont(self.bold_font)
        self.top_row_layout.addWidget(lbl_name)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("var_name")
        self.name_input.setFixedWidth(160)
        self.top_row_layout.addWidget(self.name_input)

        lbl_type = QLabel("类型")
        lbl_type.setFont(self.bold_font)
        self.top_row_layout.addWidget(lbl_type)

        self.type_combo = QComboBox()
        self.type_combo.addItems(["整数", "浮点数", "字符"])
        self.top_row_layout.addWidget(self.type_combo)

        lbl_source = QLabel("来自")
        lbl_source.setFont(self.bold_font)
        self.top_row_layout.addWidget(lbl_source)

        self.source_combo = QComboBox()
        self.source_combo.addItems(["数据范围", "列表选择", "字符集合", "来自代码"])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.source_combo.currentIndexChanged.connect(self._update_header)
        self.top_row_layout.addWidget(self.source_combo)

        lbl_loop = QLabel("循环")
        lbl_loop.setFont(self.bold_font)
        self.top_row_layout.addWidget(lbl_loop)

        self.loop_input = QLineEdit("1")
        self.loop_input.setFixedWidth(60)
        self.loop_input.setAlignment(Qt.AlignCenter)
        self.top_row_layout.addWidget(self.loop_input)

        lbl_sep = QLabel("分隔符")
        lbl_sep.setFont(self.bold_font)
        self.top_row_layout.addWidget(lbl_sep)

        self.separator_combo = QComboBox()
        self.separator_combo.addItems(["无", "空格", "换行", "制表符", "逗号", "分号"])
        self.separator_combo.setCurrentText("换行")
        self.top_row_layout.addWidget(self.separator_combo)

        self.top_row_layout.addStretch(1)
        self.details_layout.addWidget(self.top_row_widget)
        
        # --- 第二行：参数配置 (StackedWidget) ---
        self.params_stack = QStackedWidget()
        self.details_layout.addWidget(self.params_stack)
        
        self._init_param_pages()
        self._on_source_changed(0) 

        self.main_layout.addWidget(self.details_container)

        self.name_input.textChanged.connect(self._update_header)
        self.type_combo.currentIndexChanged.connect(self._update_header)
        self.loop_input.textChanged.connect(self._update_header)
        self.separator_combo.currentIndexChanged.connect(self._update_header)

        self._update_header()

    def setup_style(self):
        self.setStyleSheet(VARIABLE_ROW_QSS)

    def _init_param_pages(self):
        """初始化不同来源对应的参数页面"""
        
        # Page 0: 数据范围 (Range)
        self.page_range = QWidget()
        layout_range = QHBoxLayout(self.page_range)
        layout_range.setContentsMargins(0, 5, 0, 0) # 顶部稍微留白
        layout_range.setSpacing(8)
        layout_range.setAlignment(Qt.AlignLeft)
        
        lbl_range = QLabel("范围:")
        lbl_range.setFont(self.bold_font)
        layout_range.addWidget(lbl_range)
        
        self.range_min = QLineEdit("1")
        self.range_min.setFixedWidth(80)
        self.range_min.setAlignment(Qt.AlignCenter)
        layout_range.addWidget(self.range_min)
        
        layout_range.addWidget(QLabel("至"))
        
        self.range_max = QLineEdit("100")
        self.range_max.setFixedWidth(80)
        self.range_max.setAlignment(Qt.AlignCenter)
        layout_range.addWidget(self.range_max)
        
        self.params_stack.addWidget(self.page_range)
        self.range_min.textChanged.connect(self._update_header)
        self.range_max.textChanged.connect(self._update_header)
        
        # Page 1: 列表选择 (List)
        self.page_list = QWidget()
        layout_list = QHBoxLayout(self.page_list)
        layout_list.setContentsMargins(0, 5, 0, 0)
        layout_list.setSpacing(8)
        layout_list.setAlignment(Qt.AlignLeft)
        
        lbl_list = QLabel("列表项 (逗号分隔):")
        lbl_list.setFont(self.bold_font)
        layout_list.addWidget(lbl_list)
        
        self.list_input = QLineEdit()
        self.list_input.setPlaceholderText("Apple, Banana, Orange")
        layout_list.addWidget(self.list_input)
        self.params_stack.addWidget(self.page_list)
        self.list_input.textChanged.connect(self._update_header)
        
        # Page 2: 字符集合 (Charset)
        self.page_charset = QWidget()
        layout_charset = QHBoxLayout(self.page_charset)
        layout_charset.setContentsMargins(0, 5, 0, 0)
        layout_charset.setSpacing(8)
        layout_charset.setAlignment(Qt.AlignLeft)
        
        lbl_len = QLabel("长度:")
        lbl_len.setFont(self.bold_font)
        layout_charset.addWidget(lbl_len)
        
        self.char_len_input = QLineEdit("10")
        self.char_len_input.setFixedWidth(60)
        layout_charset.addWidget(self.char_len_input)
        
        lbl_set = QLabel("字符集:")
        lbl_set.setFont(self.bold_font)
        layout_charset.addWidget(lbl_set)
        
        self.charset_combo = QComboBox()
        self.charset_combo.addItems(["a-z", "A-Z", "0-9", "a-zA-Z0-9", "自定义"])
        layout_charset.addWidget(self.charset_combo)
        
        self.params_stack.addWidget(self.page_charset)
        self.char_len_input.textChanged.connect(self._update_header)
        self.charset_combo.currentIndexChanged.connect(self._update_header)

        # Page 3: 来自代码 (Code)
        self.page_code = QWidget()
        layout_code = QVBoxLayout(self.page_code)
        layout_code.setContentsMargins(0, 5, 0, 0)
        layout_code.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        lbl_code = QLabel("Python 表达式")
        lbl_code.setFont(self.bold_font)
        header_row.addWidget(lbl_code)
        header_row.addStretch(1)

        layout_code.addLayout(header_row)

        self.code_preview = ClickableLineEdit()
        self.code_preview.setReadOnly(True)
        self.code_preview.setProperty("codePreview", True)
        self.code_preview.setPlaceholderText("点击打开代码编辑器…")
        self.code_preview.clicked.connect(self._open_code_editor_dialog)
        layout_code.addWidget(self.code_preview)

        self.params_stack.addWidget(self.page_code)
        self._sync_code_preview()

    def _set_expanded(self, expanded: bool):
        self.details_container.setVisible(expanded)
        self.expand_btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)

    def _update_header(self):
        name = self.name_input.text().strip()
        self.title_label.setText(name if name else "未命名变量")

        var_type = self.type_combo.currentText()
        source = self.source_combo.currentText()
        loop = self.loop_input.text().strip() or "1"
        sep_ui = self.separator_combo.currentText()

        parts = [var_type, source]

        idx = self.source_combo.currentIndex()
        if idx == 0:
            vmin = self.range_min.text().strip()
            vmax = self.range_max.text().strip()
            if vmin or vmax:
                parts.append(f"{vmin or '…'}–{vmax or '…'}")
        elif idx == 1:
            raw = self.list_input.text().strip()
            if raw:
                items = [s.strip() for s in raw.split(",") if s.strip()]
                parts.append(f"{len(items)} 项")
            else:
                parts.append("空列表")
        elif idx == 2:
            length = self.char_len_input.text().strip()
            charset = self.charset_combo.currentText()
            if length:
                parts.append(f"len {length}")
            parts.append(charset)
        elif idx == 3:
            first_line = self._first_non_empty_line(self._code_text)
            if first_line:
                parts.append((first_line[:28] + "…") if len(first_line) > 28 else first_line)

        parts.append(f"循环 {loop}")
        parts.append(f"分隔符 {sep_ui}")
        summary = " · ".join(parts)
        self.summary_label.setText(summary)
        self.summary_label.setToolTip(summary)
        self.title_label.setToolTip(self.title_label.text())
        
    def _on_source_changed(self, index):
        self.params_stack.setCurrentIndex(index)

    def _on_delete_clicked(self):
        self.delete_requested.emit(self)

    def get_config(self) -> VariableConfig:
        """获取当前行的配置对象"""
        name = self.name_input.text().strip()
        
        # 映射回内部英文 Key
        ui_type = self.type_combo.currentText()
        var_type = self.type_map.get(ui_type, "String")
        
        ui_source = self.source_combo.currentText()
        source_type = self.source_map.get(ui_source, "Range")
        
        ui_sep = self.separator_combo.currentText()
        separator = self.separator_map.get(ui_sep, "\n")
        
        try:
            loop_count = int(self.loop_input.text())
        except ValueError:
            loop_count = 1
            
        params = {}
        idx = self.source_combo.currentIndex()
        if idx == 0: # 数据范围
            params['min'] = self.range_min.text()
            params['max'] = self.range_max.text()
        elif idx == 1: # 列表选择
            params['items'] = self.list_input.text()
        elif idx == 2: # 字符集合
            params['length'] = self.char_len_input.text()
            params['charset'] = self.charset_combo.currentText()
        elif idx == 3: # 来自代码
            params['code'] = self._code_text
            
        return VariableConfig(
            name=name,
            var_type=var_type,
            source_type=source_type,
            params=params,
            separator=separator,
            loop_count=loop_count,
            dependencies=[] 
        )

    def _open_code_editor_dialog(self):
        dlg = CodeEditorDialog(self, title="代码编辑器")
        dlg.set_mode("generate_data")
        if self._code_text.strip():
            dlg.set_text(self._code_text)
        else:
            dlg.reset_template()
        if dlg.exec():
            self._code_text = dlg.get_text()
            self._sync_code_preview()
            self._update_header()

    def _sync_code_preview(self):
        first_line = self._first_non_empty_line(self._code_text)
        if first_line:
            shown = (first_line[:120] + "…") if len(first_line) > 120 else first_line
            self.code_preview.setText(shown)
            self.code_preview.setToolTip(first_line)
        else:
            self.code_preview.clear()
            self.code_preview.setToolTip("")

    def _first_non_empty_line(self, text: str) -> str:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s:
                return s
        return ""
