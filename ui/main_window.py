import time
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStatusBar, QSplitter, QDialog
from PySide6.QtCore import Qt, Slot, QSettings, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from .styles.main_window_style import MAIN_WINDOW_QSS
from .widgets.preview_dialog import PreviewDialog
from .widgets.problem_workbench_dialog import ProblemWorkbenchDialog
from .widgets.variables_panel import VariablesPanel
from .widgets.generation_panel import GenerationPanel
from .widgets.common.update_dialog import UpdateDialog
from services.preview.preview_sample import build_problem_set
from services.score_distributor import distribute_scores
from services.update.update_service import fetch_update_info, get_current_version, is_newer_version
from services.update import update_config


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1200, 850)
        self._last_generated_samples = []

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(20, 20, 20, 20)

        self.setup_style()
        self._init_header()
        self._init_body()
        self._init_status_bar()
        self._load_settings()
        QTimer.singleShot(400, self._check_updates)

    def closeEvent(self, event: QCloseEvent):
        self._save_settings()
        super().closeEvent(event)

    def setup_style(self):
        self.setStyleSheet(MAIN_WINDOW_QSS)

    def _init_header(self):
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 12)
        header_layout.setSpacing(16)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        title_label = QLabel("数据生成器配置")
        title_label.setObjectName("HeaderTitle")
        left_layout.addWidget(title_label)

        hint_label = QLabel("配置变量列表 → 设置生成参数 → 生成 / 预览")
        hint_label.setObjectName("HeaderHint")
        left_layout.addWidget(hint_label)

        header_layout.addWidget(left)
        header_layout.addStretch()
        self.main_layout.addWidget(header)

    def _init_body(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.variables_panel = VariablesPanel(self)
        self.generation_panel = GenerationPanel(self)
        self.generation_panel.preview_requested.connect(self.on_preview_clicked)
        self.generation_panel.generate_requested.connect(self.on_generate_clicked)
        self.generation_panel.status_message.connect(self._on_status_message)

        splitter.addWidget(self.variables_panel)
        splitter.addWidget(self.generation_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 420])

        self.main_layout.addWidget(splitter, 1)

    def _init_status_bar(self):
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet("QStatusBar { background-color: #f5f5f7; color: #666; }")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    @Slot(str)
    def _on_status_message(self, msg: str) -> None:
        self.status_bar.showMessage(str(msg or ""))

    @Slot()
    def on_preview_clicked(self):
        self.status_bar.showMessage("正在预览...")

        configs = self.variables_panel.get_configs()
        total_rows = self.generation_panel.get_total_rows()

        dlg = PreviewDialog(
            self,
            configs=configs,
            total_rows=total_rows,
            no_duplicates=self.generation_panel.get_no_duplicates(),
        )
        dlg.exec()
        self.status_bar.showMessage("就绪")

    @Slot()
    def on_generate_clicked(self):
        self.status_bar.showMessage("正在生成...")

        configs = self.variables_panel.get_configs()
        total_rows = self.generation_panel.get_total_rows()

        problem_set = build_problem_set(
            configs,
            total_rows,
            no_duplicates=self.generation_panel.get_no_duplicates(),
        )
        cases = list((problem_set or {}).get("cases") or [])

        if self.generation_panel.get_use_score() and cases:
            total_score = self.generation_panel.get_total_score()
            scores = distribute_scores(total_score, cases)

            for i, case in enumerate(cases):
                if i < len(scores):
                    case["score"] = scores[i]

        self._last_generated_samples = cases
        self.status_bar.showMessage(f"已生成 {len(cases)} 条")

        dlg = ProblemWorkbenchDialog(self, problem_set=problem_set)
        dlg.exec()

    def get_last_generated_samples(self):
        return list(self._last_generated_samples or [])

    def _load_settings(self):
        settings = QSettings("DataGenerator", "Config")
        self.generation_panel.load_from_qsettings(settings)

    def _save_settings(self):
        settings = QSettings("DataGenerator", "Config")
        self.generation_panel.save_to_qsettings(settings)

    def _check_updates(self):
        if not self._should_check_updates():
            return
        info = fetch_update_info()
        if not info:
            return
        current_version = get_current_version()
        if not is_newer_version(info.latest_version, current_version):
            self._mark_update_checked()
            return
        dlg = UpdateDialog(
            self,
            current_version=current_version,
            latest_version=info.latest_version,
            notes=info.notes,
            download_url=info.download_url,
            published_at=info.published_at,
        )
        result = dlg.exec()
        if result == QDialog.Accepted and info.download_url:
            self._mark_update_checked()
            QDesktopServices.openUrl(QUrl(info.download_url))

    def _should_check_updates(self) -> bool:
        settings = QSettings("DataGenerator", "Config")
        last_checked = settings.value("update/last_checked", 0)
        try:
            last_checked = float(last_checked or 0)
        except Exception:
            last_checked = 0
        now = time.time()
        return (now - last_checked) >= float(update_config.UPDATE_CHECK_INTERVAL_SECONDS)

    def _mark_update_checked(self) -> None:
        settings = QSettings("DataGenerator", "Config")
        settings.setValue("update/last_checked", time.time())
