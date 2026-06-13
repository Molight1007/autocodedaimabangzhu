from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QFrame, QToolButton, QLayout,
    QMessageBox, QMenu
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QFont, QIcon
from core.models import VariableConfig
from services.preview.distribution_registry import get_distribution_registry
from utils.text_syntax import parse_escaped_csv
from ..styles.variable_row_style import VARIABLE_ROW_QSS
from .common.auto_sizing_stacked_widget import AutoSizingStackedWidget
from ._variable_row.segment_rule_row import SegmentRuleRow
from ._variable_row.code_source_widget import CodeSourceWidget


class VariableRowWidget(QFrame):
    """
    原子化变量配置行组件 (Modern Style)。
    """
    # 信号：请求删除自身
    delete_requested = Signal(QWidget)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VariableRow")
        
        # 汉化映射字典
        self.type_map = {"整数": "Integer", "浮点数": "Float", "字符": "String"}
        self.source_map = {"数据范围": "Range", "列表选择": "List", "字符集合": "Charset", "来自代码": "Code"}
        self.separator_map = {"无": "", "空格": " ", "换行": "\n", "制表符": "\t", "逗号": ",", "分号": ";"}
        self._distribution_registry = get_distribution_registry()
        self._current_distribution_key = "uniform"
        self._segment_mode_map = {"范围": "range", "固定值": "fixed", "边界值": "edge"}
        self._segment_rule_rows = []
        
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
        self.details_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)

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
        self.loop_input.setPlaceholderText("1 or 1,1000")
        self.loop_input.setToolTip("支持固定次数、变量表达式，或使用 1,1000 表示随机循环次数区间")
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
        self.params_stack = AutoSizingStackedWidget()
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
        layout_range = QVBoxLayout(self.page_range)
        layout_range.setContentsMargins(0, 5, 0, 0)
        layout_range.setSpacing(8)
        layout_range.setSizeConstraint(QLayout.SetMinAndMaxSize)

        range_row = QHBoxLayout()
        range_row.setContentsMargins(0, 0, 0, 0)
        range_row.setSpacing(8)
        range_row.setAlignment(Qt.AlignLeft)
        
        lbl_range = QLabel("范围:")
        lbl_range.setFont(self.bold_font)
        range_row.addWidget(lbl_range)
        
        self.range_min = QLineEdit("1")
        self.range_min.setFixedWidth(80)
        self.range_min.setAlignment(Qt.AlignCenter)
        range_row.addWidget(self.range_min)
        
        range_row.addWidget(QLabel("至"))
        
        self.range_max = QLineEdit("100")
        self.range_max.setFixedWidth(80)
        self.range_max.setAlignment(Qt.AlignCenter)
        range_row.addWidget(self.range_max)
        range_row.addStretch(1)
        layout_range.addLayout(range_row)

        strategy_row = QHBoxLayout()
        strategy_row.setContentsMargins(0, 0, 0, 0)
        strategy_row.setSpacing(8)
        strategy_row.setAlignment(Qt.AlignLeft)

        lbl_strategy = QLabel("分布策略:")
        lbl_strategy.setFont(self.bold_font)
        strategy_row.addWidget(lbl_strategy)

        self.range_distribution_btn = QPushButton("")
        self.range_distribution_btn.setCursor(Qt.PointingHandCursor)
        self.range_distribution_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #e0e0e0;
                border-radius: 3px;
                padding: 4px 24px 4px 10px;
                background-color: #fcfcfc;
                text-align: left;
            }
            QPushButton:hover {
                border: 1px solid #007AFF;
            }
        """)
        self.range_distribution_btn.clicked.connect(self._show_distribution_menu)
        strategy_row.addWidget(self.range_distribution_btn)

        self.range_distribution_help_btn = QPushButton("?")
        self.range_distribution_help_btn.setFixedSize(26, 26)
        self.range_distribution_help_btn.setCursor(Qt.PointingHandCursor)
        self.range_distribution_help_btn.setToolTip("查看分布说明")
        self.range_distribution_help_btn.setStyleSheet("QPushButton { font-weight: bold; border-radius: 13px; }")
        self.range_distribution_help_btn.clicked.connect(self._show_current_distribution_help)
        strategy_row.addWidget(self.range_distribution_help_btn)
        strategy_row.addStretch(1)
        layout_range.addLayout(strategy_row)

        self.range_distribution_stack = AutoSizingStackedWidget()
        layout_range.addWidget(self.range_distribution_stack)
        self._init_range_distribution_pages()
        
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
        self.charset_combo.setEditable(True)
        self.charset_combo.addItems([
            "a-z",
            "A-Z",
            "0-9",
            "a-zA-Z0-9",
            "A-Z,a-z,0-9",
            r"A-Z,a-z,0-9,。,！,？,<,>",
        ])
        self.charset_combo.setCurrentText("a-zA-Z0-9")
        self.charset_combo.setToolTip("支持 A-Z / a-z / 0-9 关键字，也支持逗号分隔的字面量；英文逗号请写成 \\,")
        if self.charset_combo.lineEdit() is not None:
            self.charset_combo.lineEdit().setPlaceholderText(r"A-Z,a-z,0-9,。,！,？,<,> 或 hello,world,\,")
        layout_charset.addWidget(self.charset_combo)
        
        self.params_stack.addWidget(self.page_charset)
        self.char_len_input.textChanged.connect(self._update_header)
        self.charset_combo.currentTextChanged.connect(self._update_header)

        # Page 3: 来自代码 (Code)
        self.page_code = CodeSourceWidget()
        self.page_code.changed.connect(self._update_header)
        self.params_stack.addWidget(self.page_code)

    def _init_range_distribution_pages(self):
        self.range_distribution_page_basic = QWidget()
        self.range_distribution_stack.addWidget(self.range_distribution_page_basic)

        self.range_distribution_page_advanced = QWidget()
        self.range_advanced_params_layout = QHBoxLayout(self.range_distribution_page_advanced)
        self.range_advanced_params_layout.setContentsMargins(0, 0, 0, 0)
        self.range_advanced_params_layout.setSpacing(8)
        self._current_param_inputs: dict = {}
        self.range_distribution_stack.addWidget(self.range_distribution_page_advanced)

        self.range_distribution_page_segments = QWidget()
        segments_layout = QVBoxLayout(self.range_distribution_page_segments)
        segments_layout.setContentsMargins(0, 0, 0, 0)
        segments_layout.setSpacing(6)
        segments_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)

        segments_hint = QLabel("每条规则填写权重与生成方式，概率会按总权重自动折算。")
        segments_layout.addWidget(segments_hint)

        self.segment_rules_container = QWidget()
        self.segment_rules_layout = QVBoxLayout(self.segment_rules_container)
        self.segment_rules_layout.setContentsMargins(0, 0, 0, 0)
        self.segment_rules_layout.setSpacing(6)
        self.segment_rules_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
        segments_layout.addWidget(self.segment_rules_container)

        self.add_segment_rule_btn = QPushButton("+ 添加规则")
        self.add_segment_rule_btn.setCursor(Qt.PointingHandCursor)
        self.add_segment_rule_btn.clicked.connect(self._add_segment_rule)
        segments_layout.addWidget(self.add_segment_rule_btn, 0, Qt.AlignLeft)

        self.range_distribution_stack.addWidget(self.range_distribution_page_segments)

        self._apply_distribution_selection()

    def _apply_distribution_selection(self):
        distribution = self._current_distribution_key
        self._update_distribution_btn_text()
        entry = self._distribution_registry.get_entry(distribution)
        if entry and entry.special_ui == "segments":
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_segments)
            if not self._segment_rule_rows:
                self._add_segment_rule()
        elif entry and entry.params:
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_advanced)
            self._update_advanced_distribution_fields()
        else:
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_basic)
        self._refresh_dynamic_layout()
        self._update_header()

    def _update_distribution_btn_text(self):
        entry = self._distribution_registry.get_entry(self._current_distribution_key)
        text = entry.display_name if entry else self._current_distribution_key
        self.range_distribution_btn.setText(f"  {text}  ▼")

    def _show_distribution_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: white;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                padding: 4px 0px;
            }
            QMenu::item {
                padding: 6px 30px 6px 12px;
                color: #333333;
            }
            QMenu::item:selected {
                background-color: #0078d7;
                color: white;
            }
            QMenu::separator {
                height: 1px;
                background: #e0e0e0;
                margin: 4px 8px;
            }
        """)
        grouped = self._distribution_registry.get_grouped_entries()
        for item in grouped:
            if item["type"] == "item":
                entry = item["entry"]
                action = menu.addAction(entry.display_name)
                action.triggered.connect(lambda checked=False, k=entry.key: self._on_distribution_selected(k))
            elif item["type"] == "group":
                sub_menu = menu.addMenu(item["name"] + " ▸")
                for entry in item["entries"]:
                    action = sub_menu.addAction(entry.name)
                    action.triggered.connect(lambda checked=False, k=entry.key: self._on_distribution_selected(k))
        menu.exec_(self.range_distribution_btn.mapToGlobal(self.range_distribution_btn.rect().bottomLeft()))

    def _on_distribution_selected(self, key: str):
        if self._current_distribution_key == key:
            return
        self._current_distribution_key = key
        self._apply_distribution_selection()

    def _show_current_distribution_help(self):
        entry = self._distribution_registry.get_entry(self._current_distribution_key)
        if entry and entry.description:
            QMessageBox.information(self, f"分布说明 - {entry.display_name}", entry.description)
        else:
            QMessageBox.information(self, "提示", "该分布暂无详细说明。")

    def _on_range_distribution_changed(self, index):
        pass

    def _clear_advanced_params(self):
        self._current_param_inputs.clear()
        layout = self.range_advanced_params_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _update_advanced_distribution_fields(self):
        self._clear_advanced_params()
        distribution = self._current_range_distribution()
        entry = self._distribution_registry.get_entry(distribution)
        if entry is None or not entry.params:
            return

        for p_def in entry.params:
            label = QLabel(p_def["label"])
            label.setFont(self.bold_font)
            input_field = QLineEdit()
            input_field.setFixedWidth(100)
            placeholder = p_def.get("placeholder", "")
            if placeholder:
                input_field.setPlaceholderText(placeholder)
            input_field.textChanged.connect(self._update_header)
            self._current_param_inputs[p_def["key"]] = input_field
            self.range_advanced_params_layout.addWidget(label)
            self.range_advanced_params_layout.addWidget(input_field)

        self.range_advanced_params_layout.addStretch(1)

    def _add_segment_rule(self):
        row = SegmentRuleRow(
            distribution_registry=self._distribution_registry,
            segment_mode_map=self._segment_mode_map,
            parent=self,
        )
        self._segment_rule_rows.append(row)
        self.segment_rules_layout.addWidget(row)
        row.changed.connect(self._update_header)
        row.changed.connect(self._refresh_dynamic_layout)
        row.remove_requested.connect(self._remove_segment_rule)
        self._refresh_dynamic_layout()

    def _remove_segment_rule(self, row):
        if row not in self._segment_rule_rows:
            return
        self._segment_rule_rows.remove(row)
        row.deleteLater()
        self.segment_rules_layout.removeWidget(row)
        if not self._segment_rule_rows:
            self._add_segment_rule()
        self._update_header()
        self._refresh_dynamic_layout()

    def _current_range_distribution(self):
        return self._current_distribution_key

    def _refresh_dynamic_layout(self):
        for widget in (
            self.segment_rules_container,
            self.range_distribution_page_segments,
            self.range_distribution_stack,
            self.page_range,
            self.params_stack,
            self.details_container,
            self,
        ):
            if widget is None:
                continue
            widget.updateGeometry()
            widget.adjustSize()

        for layout in (
            self.segment_rules_layout,
            self.details_layout,
            self.main_layout,
        ):
            if layout is None:
                continue
            layout.invalidate()
            layout.activate()

        parent = self.parentWidget()
        while parent is not None:
            if parent.layout() is not None:
                parent.layout().invalidate()
                parent.layout().activate()
            parent.updateGeometry()
            parent = parent.parentWidget()

    def _collect_range_distribution_params(self):
        distribution = self._current_range_distribution()
        params = {"distribution": distribution}

        if distribution == "custom_segments":
            segments = [row.get_data() for row in self._segment_rule_rows]
            params["segments"] = segments
            return params

        entry = self._distribution_registry.get_entry(distribution)
        if entry and entry.params:
            distribution_params = {}
            for p_def in entry.params:
                key = p_def["key"]
                input_field = self._current_param_inputs.get(key)
                value = ""
                if input_field is not None:
                    value = input_field.text().strip()
                if value:
                    distribution_params[key] = value
                elif p_def.get("default") is not None:
                    distribution_params[key] = p_def["default"]
            if distribution_params:
                params["distribution_params"] = distribution_params

        return params

    def _range_summary(self) -> str:
        parts = []
        vmin = self.range_min.text().strip()
        vmax = self.range_max.text().strip()
        if vmin or vmax:
            parts.append(f"{vmin or '…'}–{vmax or '…'}")
        distribution = self._current_range_distribution()
        entry = self._distribution_registry.get_entry(distribution)
        parts.append(entry.display_name if entry else "均匀随机")
        if distribution == "custom_segments":
            parts.append(f"{len(self._segment_rule_rows)} 条规则")
        return " · ".join(parts)

    def _range_params(self) -> dict:
        params = {
            "min": self.range_min.text(),
            "max": self.range_max.text(),
        }
        params.update(self._collect_range_distribution_params())
        return params

    def _list_summary(self) -> str:
        raw = self.list_input.text().strip()
        if raw:
            items = parse_escaped_csv(raw)
            return f"{len(items)} 项"
        return "空列表"

    def _list_params(self) -> dict:
        return {"items": self.list_input.text()}

    def _charset_summary(self) -> str:
        parts = []
        length = self.char_len_input.text().strip()
        charset = self.charset_combo.currentText()
        if length:
            parts.append(f"len {length}")
        parts.append(charset)
        return " · ".join(parts)

    def _charset_params(self) -> dict:
        return {
            "length": self.char_len_input.text(),
            "charset": self.charset_combo.currentText(),
        }

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
        source_summary = {
            0: self._range_summary,
            1: self._list_summary,
            2: self._charset_summary,
            3: lambda: self.page_code.summary(),
        }.get(idx, lambda: "")()
        if source_summary:
            parts.append(source_summary)

        parts.append(f"循环 {loop}")
        parts.append(f"分隔符 {sep_ui}")
        summary = " · ".join(parts)
        self.summary_label.setText(summary)
        self.summary_label.setToolTip(summary)
        self.title_label.setToolTip(self.title_label.text())
        
    def _on_source_changed(self, index):
        self.params_stack.setCurrentIndex(index)
        self._refresh_dynamic_layout()

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
        
        loop_count = self.loop_input.text().strip() or "1"
            
        params: dict = {}
        idx = self.source_combo.currentIndex()
        params_getters = {
            0: self._range_params,
            1: self._list_params,
            2: self._charset_params,
            3: lambda: self.page_code.get_params(),
        }
        getter = params_getters.get(idx)
        if getter:
            params = getter()
            
        return VariableConfig(
            name=name,
            var_type=var_type,
            source_type=source_type,
            params=params,
            separator=separator,
            loop_count=loop_count,
            dependencies=[] 
        )
