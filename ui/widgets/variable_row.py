from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QStackedWidget, QFrame, QToolButton, QLayout
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QFont, QIcon
from core.models import VariableConfig
from services.preview.range_distribution import supported_distributions
from ..styles.variable_row_style import VARIABLE_ROW_QSS
from .code_editor_dialog import CodeEditorDialog
from .common.clickable_line_edit import ClickableLineEdit


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
        full_distribution_map = {
            "均匀随机": "uniform",
            "偏向小值": "biased_low",
            "偏向大值": "biased_high",
            "边界强化": "edge_focused",
            "高级 / 高斯分布": "gaussian",
            "高级 / 泊松分布": "poisson",
            "高级 / 指数分布": "exponential",
            "高级 / 幂律分布": "power_law",
            "自定义分段概率": "custom_segments",
        }
        available_distributions = set(supported_distributions())
        self.range_distribution_map = {
            label: key for label, key in full_distribution_map.items() if key in available_distributions
        }
        self._distribution_label_map = {v: k for k, v in self.range_distribution_map.items()}
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

        self.range_distribution_combo = QComboBox()
        self.range_distribution_combo.addItems(list(self.range_distribution_map.keys()))
        strategy_row.addWidget(self.range_distribution_combo)
        strategy_row.addStretch(1)
        layout_range.addLayout(strategy_row)

        self.range_distribution_stack = AutoSizingStackedWidget()
        layout_range.addWidget(self.range_distribution_stack)
        self._init_range_distribution_pages()
        
        self.params_stack.addWidget(self.page_range)
        self.range_min.textChanged.connect(self._update_header)
        self.range_max.textChanged.connect(self._update_header)
        self.range_distribution_combo.currentIndexChanged.connect(self._on_range_distribution_changed)
        self.range_distribution_combo.currentIndexChanged.connect(self._update_header)
        
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

    def _init_range_distribution_pages(self):
        self.range_distribution_page_basic = QWidget()
        basic_layout = QHBoxLayout(self.range_distribution_page_basic)
        basic_layout.setContentsMargins(0, 0, 0, 0)
        basic_layout.addWidget(QLabel("当前策略无需额外参数。"))
        basic_layout.addStretch(1)
        self.range_distribution_stack.addWidget(self.range_distribution_page_basic)

        self.range_distribution_page_advanced = QWidget()
        advanced_layout = QHBoxLayout(self.range_distribution_page_advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(8)

        self.range_advanced_label_primary = QLabel("参数")
        advanced_layout.addWidget(self.range_advanced_label_primary)

        self.range_advanced_input_primary = QLineEdit()
        self.range_advanced_input_primary.setFixedWidth(100)
        advanced_layout.addWidget(self.range_advanced_input_primary)

        self.range_advanced_label_secondary = QLabel("参数2")
        advanced_layout.addWidget(self.range_advanced_label_secondary)

        self.range_advanced_input_secondary = QLineEdit()
        self.range_advanced_input_secondary.setFixedWidth(100)
        advanced_layout.addWidget(self.range_advanced_input_secondary)
        advanced_layout.addStretch(1)

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

        self.range_advanced_input_primary.textChanged.connect(self._update_header)
        self.range_advanced_input_secondary.textChanged.connect(self._update_header)

        self._update_advanced_distribution_fields()
        self._on_range_distribution_changed(self.range_distribution_combo.currentIndex())

    def _on_range_distribution_changed(self, index):
        distribution = self._current_range_distribution()
        if distribution == "custom_segments":
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_segments)
            if not self._segment_rule_rows:
                self._add_segment_rule()
        elif distribution in {"gaussian", "poisson", "exponential", "power_law"}:
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_advanced)
            self._update_advanced_distribution_fields()
        else:
            self.range_distribution_stack.setCurrentWidget(self.range_distribution_page_basic)
        self._refresh_dynamic_layout()

    def _update_advanced_distribution_fields(self):
        distribution = self._current_range_distribution()
        if distribution == "gaussian":
            self.range_advanced_label_primary.setText("均值")
            self.range_advanced_input_primary.setPlaceholderText("如 50")
            self.range_advanced_label_secondary.setVisible(True)
            self.range_advanced_input_secondary.setVisible(True)
            self.range_advanced_label_secondary.setText("标准差")
            self.range_advanced_input_secondary.setPlaceholderText("如 10")
            return
        if distribution == "poisson":
            self.range_advanced_label_primary.setText("lambda")
            self.range_advanced_input_primary.setPlaceholderText("如 4")
            self.range_advanced_label_secondary.setVisible(False)
            self.range_advanced_input_secondary.setVisible(False)
            return
        if distribution == "exponential":
            self.range_advanced_label_primary.setText("lambda")
            self.range_advanced_input_primary.setPlaceholderText("如 1.5")
            self.range_advanced_label_secondary.setVisible(False)
            self.range_advanced_input_secondary.setVisible(False)
            return
        if distribution == "power_law":
            self.range_advanced_label_primary.setText("alpha")
            self.range_advanced_input_primary.setPlaceholderText("如 2.0")
            self.range_advanced_label_secondary.setVisible(False)
            self.range_advanced_input_secondary.setVisible(False)
            return

        self.range_advanced_label_primary.setText("参数")
        self.range_advanced_input_primary.setPlaceholderText("")
        self.range_advanced_label_secondary.setVisible(False)
        self.range_advanced_input_secondary.setVisible(False)

    def _add_segment_rule(self):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        prob_input = QLineEdit("30")
        prob_input.setFixedWidth(60)
        prob_input.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(prob_input)

        prob_suffix = QLabel("%")
        row_layout.addWidget(prob_suffix)

        mode_combo = QComboBox()
        mode_combo.addItems(list(self._segment_mode_map.keys()))
        row_layout.addWidget(mode_combo)

        value_input_a = QLineEdit()
        value_input_a.setFixedWidth(100)
        row_layout.addWidget(value_input_a)

        between_label = QLabel("~")
        row_layout.addWidget(between_label)

        value_input_b = QLineEdit()
        value_input_b.setFixedWidth(100)
        row_layout.addWidget(value_input_b)

        delete_btn = QPushButton("删除")
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setFixedHeight(28)
        row_layout.addWidget(delete_btn)
        row_layout.addStretch(1)

        rule = {
            "widget": row_widget,
            "prob_input": prob_input,
            "mode_combo": mode_combo,
            "value_input_a": value_input_a,
            "between_label": between_label,
            "value_input_b": value_input_b,
            "delete_btn": delete_btn,
        }
        self._segment_rule_rows.append(rule)
        self.segment_rules_layout.addWidget(row_widget)

        mode_combo.currentIndexChanged.connect(lambda _: self._update_segment_rule_mode(rule))
        mode_combo.currentIndexChanged.connect(lambda _: self._update_header())
        prob_input.textChanged.connect(self._update_header)
        value_input_a.textChanged.connect(self._update_header)
        value_input_b.textChanged.connect(self._update_header)
        delete_btn.clicked.connect(lambda: self._remove_segment_rule(rule))

        self._update_segment_rule_mode(rule)
        self._refresh_dynamic_layout()

    def _remove_segment_rule(self, rule):
        if rule not in self._segment_rule_rows:
            return
        self._segment_rule_rows.remove(rule)
        rule["widget"].deleteLater()
        self.segment_rules_layout.removeWidget(rule["widget"])
        if not self._segment_rule_rows:
            self._add_segment_rule()
        self._update_header()
        self._refresh_dynamic_layout()

    def _update_segment_rule_mode(self, rule):
        mode = self._segment_mode_map.get(rule["mode_combo"].currentText(), "range")
        if mode == "range":
            rule["value_input_a"].setVisible(True)
            rule["between_label"].setVisible(True)
            rule["value_input_b"].setVisible(True)
            rule["value_input_a"].setPlaceholderText("最小值")
            rule["value_input_b"].setPlaceholderText("最大值")
            self._refresh_dynamic_layout()
            return
        if mode == "fixed":
            rule["value_input_a"].setVisible(True)
            rule["between_label"].setVisible(False)
            rule["value_input_b"].setVisible(False)
            rule["value_input_a"].setPlaceholderText("固定值")
            self._refresh_dynamic_layout()
            return

        rule["value_input_a"].setVisible(True)
        rule["between_label"].setVisible(False)
        rule["value_input_b"].setVisible(False)
        rule["value_input_a"].setPlaceholderText("留空使用默认边界")
        self._refresh_dynamic_layout()

    def _current_range_distribution(self):
        return self.range_distribution_map.get(self.range_distribution_combo.currentText(), "uniform")

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

        if distribution == "edge_focused":
            params["edge_bias"] = 0.6
            return params

        if distribution in {"gaussian", "poisson", "exponential", "power_law"}:
            advanced_params = {}
            primary = self.range_advanced_input_primary.text().strip()
            secondary = self.range_advanced_input_secondary.text().strip()
            if distribution == "gaussian":
                if primary:
                    advanced_params["mean"] = primary
                if secondary:
                    advanced_params["stddev"] = secondary
            elif distribution in {"poisson", "exponential"}:
                if primary:
                    advanced_params["lambda"] = primary
            elif distribution == "power_law" and primary:
                advanced_params["alpha"] = primary
            if advanced_params:
                params["distribution_params"] = advanced_params
            return params

        if distribution == "custom_segments":
            segments = []
            for rule in self._segment_rule_rows:
                mode = self._segment_mode_map.get(rule["mode_combo"].currentText(), "range")
                segment = {
                    "weight": rule["prob_input"].text().strip() or "0",
                    "mode": mode,
                }
                value_a = rule["value_input_a"].text().strip()
                value_b = rule["value_input_b"].text().strip()
                if mode == "range":
                    segment["min"] = value_a
                    segment["max"] = value_b
                elif mode == "fixed":
                    segment["value"] = value_a
                else:
                    if value_a:
                        segment["edges"] = value_a
                segments.append(segment)
            params["segments"] = segments
            return params

        return params

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
            distribution = self._current_range_distribution()
            parts.append(self._distribution_label_map.get(distribution, "均匀随机"))
            if distribution == "custom_segments":
                parts.append(f"{len(self._segment_rule_rows)} 条规则")
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
        
        try:
            loop_count = int(self.loop_input.text())
        except ValueError:
            loop_count = 1
            
        params = {}
        idx = self.source_combo.currentIndex()
        if idx == 0: # 数据范围
            params['min'] = self.range_min.text()
            params['max'] = self.range_max.text()
            params.update(self._collect_range_distribution_params())
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
