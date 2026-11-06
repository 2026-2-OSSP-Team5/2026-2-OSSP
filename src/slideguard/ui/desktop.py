import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from slideguard.bootstrap import (
    build_analyzer,
    build_report_repository,
    build_workspace_settings_repository,
)
from slideguard.domain.policy import AnalysisPolicy

APP_STYLE = """
QMainWindow, QWidget#appRoot {
    background: #f3f5f8;
    color: #1f2937;
    font-family: "Malgun Gothic";
    font-size: 12px;
}
QFrame#header, QFrame#leftPanel, QFrame#contentPanel, QFrame#detailPanel {
    background: #ffffff;
    border: 1px solid #e2e7ee;
    border-radius: 6px;
}
QFrame#header {
    border-radius: 0;
    border-left: 0;
    border-right: 0;
    border-top: 0;
}
QLabel#brand {
    color: #172033;
    font-size: 22px;
    font-weight: 700;
}
QLabel#sectionTitle {
    color: #172033;
    font-size: 16px;
    font-weight: 700;
}
QLabel#muted, QLabel#folderCaption, QLabel#fileMeta, QLabel#detailCaption {
    color: #7a8494;
}
QLabel#stateTitle {
    color: #283142;
    font-size: 17px;
    font-weight: 700;
}
QLabel#historyBanner {
    color: #245fc5;
    font-weight: 700;
}
QPushButton {
    min-height: 30px;
    padding: 0 13px;
    border: 1px solid #d9dfe8;
    border-radius: 4px;
    background: #ffffff;
    color: #364152;
}
QPushButton:hover { background: #f7f9fc; }
QPushButton:disabled { color: #a4adba; background: #eef1f5; }
QPushButton#primaryButton {
    border-color: #2468d8;
    background: #2468d8;
    color: #ffffff;
    font-weight: 700;
}
QPushButton#primaryButton:hover { background: #1959bf; }
QPushButton#primaryButton:disabled {
    border-color: #d9dfe8;
    background: #e8ecf1;
    color: #9aa4b2;
}
QPushButton#refreshButton:pressed {
    border-color: #a9b8cd;
    background: #dfe7f3;
}
QPushButton#tabButton {
    min-height: 36px;
    padding: 0 8px;
    border: 0;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: #737d8c;
    background: transparent;
}
QPushButton#tabButton[active="true"] {
    border-bottom-color: #2468d8;
    color: #2468d8;
    font-weight: 700;
}
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f8fafc;
    border: 1px solid #e1e6ed;
    border-radius: 4px;
    gridline-color: #e8ecf2;
    selection-background-color: #e7f0ff;
    selection-color: #1f2937;
    outline: 0;
}
QTableWidget::item { padding: 7px; border: 0; }
QTableWidget::item:selected {
    background: #e7f0ff;
    color: #1f2937;
    border: 0;
}
QTableWidget::item:focus { outline: none; border: 0; }
QTableWidget#fileCards::item:selected, QTableWidget#historyCards::item:selected {
    color: #175fc7;
}
QHeaderView::section {
    min-height: 34px;
    padding: 0 8px;
    border: 0;
    border-right: 1px solid #e8ecf2;
    border-bottom: 1px solid #e1e6ed;
    background: #f7f8fa;
    color: #667085;
    font-weight: 600;
}
QCheckBox { color: #4b5565; spacing: 7px; }
QProgressBar {
    max-height: 6px;
    border: 0;
    border-radius: 3px;
    background: #e5eaf1;
}
QProgressBar::chunk { border-radius: 3px; background: #2468d8; }
QStatusBar { background: #f3f5f8; color: #667085; }
"""


class WhiteCheckBox(QCheckBox):
    """Checkbox with a blue indicator and a high-contrast white check mark."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setMinimumHeight(30)

    def paintEvent(self, _event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter = QStylePainter(self)

        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            self,
        ).adjusted(1, 1, -1, -1)
        checked = self.isChecked()
        border = QColor("#2468d8" if checked else "#aab3c0")
        fill = QColor("#2468d8" if checked else "#ffffff")
        if not self.isEnabled():
            border = QColor("#c8ced7")
            fill = QColor("#e8ecf1")

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(indicator, 3, 3)
        if checked:
            painter.setPen(QPen(QColor("#ffffff"), 2.0))
            left = indicator.left() + indicator.width() * 0.22
            middle_x = indicator.left() + indicator.width() * 0.43
            middle_y = indicator.top() + indicator.height() * 0.68
            right = indicator.left() + indicator.width() * 0.80
            painter.drawLine(int(left), int(indicator.center().y()), int(middle_x), int(middle_y))
            painter.drawLine(
                int(middle_x),
                int(middle_y),
                int(right),
                int(indicator.top() + indicator.height() * 0.28),
            )

        painter.setPen(QColor("#4b5565" if self.isEnabled() else "#a4adba"))
        text_rect = self.rect().adjusted(indicator.right() + 9, 0, -1, 0)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )


class AnalysisWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    storage_failed = Signal(str)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            analyzer = build_analyzer()
            report = analyzer.execute(self.path, AnalysisPolicy())
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
            return

        self.succeeded.emit(report)
        try:
            build_report_repository().save(report)
        except Exception as error:
            self.storage_failed.emit(f"{type(error).__name__}: {error}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.workspace: Path | None = None
        self.selected_file: Path | None = None
        self.worker: AnalysisWorker | None = None
        self.current_report = None
        self.viewing_history = False

        self.setWindowTitle("SlideGuard")
        self.resize(1280, 820)
        self.setMinimumSize(980, 650)
        self.setStyleSheet(APP_STYLE)

        root = QWidget(objectName="appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(14, 14, 14, 10)
        body_layout.setSpacing(12)
        body_layout.addWidget(self._build_left_panel())
        body_layout.addWidget(self._build_content_panel(), 1)
        root_layout.addWidget(body, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("준비됨")
        self.restore_workspace_settings()
        self.refresh_history()
        self.show_pre_analysis()

    def _build_left_panel(self) -> QWidget:
        panel = QFrame(objectName="leftPanel")
        panel.setFixedWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)

        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self.files_tab_button = QPushButton("폴더 내 PPTX 파일", objectName="tabButton")
        self.history_tab_button = QPushButton("검사 이력", objectName="tabButton")
        self.files_tab_button.clicked.connect(self.show_file_tab)
        self.history_tab_button.clicked.connect(self.show_history_list)
        tabs.addWidget(self.files_tab_button)
        tabs.addWidget(self.history_tab_button)
        layout.addLayout(tabs)

        self.left_stack = QStackedWidget()
        self.files_left_page = self._build_files_left_page()
        self.history_left_page = self._build_history_left_page()
        self.left_stack.addWidget(self.files_left_page)
        self.left_stack.addWidget(self.history_left_page)
        layout.addWidget(self.left_stack, 1)
        self._set_active_tab("files")
        return panel

    def _build_files_left_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)

        self.folder_label = QLabel("작업 폴더를 선택하세요.")
        self.folder_label.setWordWrap(True)
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.folder_label)

        folder_actions = QHBoxLayout()
        self.choose_button = QPushButton("폴더 변경")
        self.choose_button.clicked.connect(self.choose_folder)
        folder_actions.addWidget(self.choose_button)
        self.recursive = WhiteCheckBox("하위 폴더 포함")
        self.recursive.toggled.connect(self.change_recursive_option)
        folder_actions.addWidget(self.recursive)
        folder_actions.addStretch()
        self.file_count_label = QLabel("0개", objectName="muted")
        folder_actions.addWidget(self.file_count_label)
        self.file_refresh_button = QPushButton(objectName="refreshButton")
        self.file_refresh_button.setFixedWidth(34)
        self.file_refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.file_refresh_button.setIconSize(QSize(16, 16))
        self.file_refresh_button.setToolTip("파일 목록 새로고침")
        self.file_refresh_button.setAccessibleName("파일 목록 새로고침")
        self.file_refresh_button.clicked.connect(self.refresh_files)
        folder_actions.addWidget(self.file_refresh_button)
        layout.addLayout(folder_actions)

        self.file_table = QTableWidget(0, 1)
        self.file_table.setObjectName("fileCards")
        self.file_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.file_table.setShowGrid(False)
        self.file_table.setWordWrap(True)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.horizontalHeader().setVisible(False)
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.file_table.currentCellChanged.connect(self.select_file)
        layout.addWidget(self.file_table, 1)

        help_text = QLabel("PPTX 파일만 표시합니다.\n파일을 선택한 뒤 검사를 실행하세요.", objectName="muted")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        return page

    def _build_history_left_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)

        actions = QHBoxLayout()
        self.history_count_label = QLabel("0건", objectName="muted")
        actions.addWidget(self.history_count_label)
        actions.addStretch()
        self.clear_history_button = QPushButton("이력 초기화")
        self.clear_history_button.clicked.connect(self.clear_history)
        self.clear_history_button.setEnabled(False)
        actions.addWidget(self.clear_history_button)
        layout.addLayout(actions)

        self.history_table = QTableWidget(0, 1)
        self.history_table.setObjectName("historyCards")
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.history_table.setShowGrid(False)
        self.history_table.setWordWrap(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.currentCellChanged.connect(self.select_history)
        layout.addWidget(self.history_table, 1)

        note = QLabel("항목을 선택하면 검사 당시 저장된 결과를 표시합니다.", objectName="muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _build_content_panel(self) -> QWidget:
        panel = QFrame(objectName="contentPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        self.inspection_page = self._build_inspection_page()
        layout.addWidget(self.inspection_page)
        return panel

    def _build_inspection_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        file_column = QVBoxLayout()
        self.selected_file_label = QLabel("PPTX 파일을 선택하세요", objectName="sectionTitle")
        self.selected_file_label.setWordWrap(True)
        self.selected_file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_meta_label = QLabel("왼쪽 목록에서 검사할 파일을 선택하세요.", objectName="fileMeta")
        file_column.addWidget(self.selected_file_label)
        file_column.addWidget(self.file_meta_label)
        top.addLayout(file_column, 1)

        self.analyze_button = QPushButton("▷ 검사 실행", objectName="primaryButton")
        self.analyze_button.clicked.connect(self.start_analysis)
        self.analyze_button.setEnabled(False)
        top.addWidget(self.analyze_button)
        layout.addLayout(top)

        self.history_banner = QLabel(objectName="historyBanner")
        self.history_banner.hide()
        layout.addWidget(self.history_banner)

        self.inspection_stack = QStackedWidget()
        self.pre_page = self._build_state_page(
            "아직 검사하지 않은 파일입니다",
            "오른쪽 위의 ‘검사 실행’을 누르면 선택한 PPTX 파일을 검사합니다.\n"
            "검사가 끝나면 슬라이드별 문제와 판정 근거를 확인할 수 있습니다.",
        )
        self.progress_page = self._build_progress_page()
        self.results_page = self._build_results_page()
        self.error_page = self._build_state_page("검사에 실패했습니다", "오류 내용을 확인한 뒤 다시 시도하세요.")
        self.history_empty_page = self._build_state_page(
            "검사 이력을 선택하세요",
            "왼쪽 이력 목록에서 항목을 선택하면 검사 당시 저장된 결과와 판정 근거를 표시합니다.",
        )
        self.inspection_stack.addWidget(self.pre_page)
        self.inspection_stack.addWidget(self.progress_page)
        self.inspection_stack.addWidget(self.results_page)
        self.inspection_stack.addWidget(self.error_page)
        self.inspection_stack.addWidget(self.history_empty_page)
        layout.addWidget(self.inspection_stack, 1)
        return page

    def _build_state_page(self, title: str, description: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(2)
        icon = QLabel("▣")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 28px; color: #9aa4b2;")
        layout.addWidget(icon)
        title_label = QLabel(title, objectName="stateTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        description_label = QLabel(description, objectName="muted")
        description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_label.setWordWrap(True)
        layout.addWidget(description_label)
        layout.addStretch(3)
        page.title_label = title_label
        page.description_label = description_label
        return page

    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(2)
        title = QLabel("선택한 파일을 검사하고 있습니다", objectName="stateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self.progress_file_label = QLabel("검사 중", objectName="muted")
        self.progress_file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_file_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumWidth(520)
        layout.addWidget(self.progress_bar, alignment=Qt.AlignmentFlag.AlignCenter)
        note = QLabel("검사가 끝나면 결과가 자동으로 표시됩니다.", objectName="muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)
        layout.addStretch(3)
        return page

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["슬라이드 번호", "검사 항목", "판정", "문제 요약"])
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setShowGrid(False)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.verticalHeader().setDefaultSectionSize(38)
        self.result_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.result_table.horizontalHeader().resizeSection(2, 112)
        self.result_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.result_table.currentCellChanged.connect(self.show_finding_details)
        layout.addWidget(self.result_table, 3)

        detail = QFrame(objectName="detailPanel")
        detail_layout = QVBoxLayout(detail)
        self.detail_title = QLabel("결과 행을 선택하면 상세 내용을 확인할 수 있습니다.", objectName="sectionTitle")
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        self.detail_values = QLabel(objectName="detailCaption")
        self.detail_values.setWordWrap(True)
        self.detail_values.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_values)
        self.detail_reason = QLabel()
        self.detail_reason.setWordWrap(True)
        self.detail_reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_reason)
        layout.addWidget(detail, 2)
        return page

    def _set_active_tab(self, name: str):
        for button, active in (
            (self.files_tab_button, name == "files"),
            (self.history_tab_button, name == "history"),
        ):
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)

    def show_file_tab(self):
        self._set_active_tab("files")
        self.left_stack.setCurrentWidget(self.files_left_page)
        self.history_banner.hide()
        self.analyze_button.show()
        if self.viewing_history:
            self.viewing_history = False
            if self.selected_file is not None:
                self.selected_file_label.setText(self.selected_file.name)
                self.file_meta_label.setText("PPTX")
            else:
                self.selected_file_label.setText("PPTX 파일을 선택하세요")
                self.file_meta_label.setText("왼쪽 목록에서 검사할 파일을 선택하세요.")
            self.show_pre_analysis()
        elif self.current_report is None:
            self.show_pre_analysis()

    def show_history_list(self):
        if self.worker is not None:
            return
        self._set_active_tab("history")
        self.left_stack.setCurrentWidget(self.history_left_page)
        self.analyze_button.hide()
        self.refresh_history()
        if self.history_table.currentRow() < 0:
            self._show_empty_history_detail()

    def show_pre_analysis(self):
        self.current_report = None
        self.result_table.setRowCount(0)
        self.detail_title.setText("결과 행을 선택하면 상세 내용을 확인할 수 있습니다.")
        self.detail_values.clear()
        self.detail_reason.clear()
        self.inspection_stack.setCurrentWidget(self.pre_page)

    def choose_folder(self):
        if self.worker is not None:
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "관리할 PPTX 작업 폴더 선택",
            str(self.workspace) if self.workspace else "",
        )
        if not folder:
            return

        self.workspace = Path(folder)
        self._show_workspace_name()
        self.selected_file = None
        self.selected_file_label.setText("PPTX 파일을 선택하세요")
        self.file_meta_label.setText("왼쪽 목록에서 검사할 파일을 선택하세요.")
        self.analyze_button.setEnabled(False)
        self.show_pre_analysis()
        self.save_workspace_settings()
        self.refresh_files()

    def _show_workspace_name(self):
        if self.workspace is None:
            self.folder_label.setText("작업 폴더를 선택하세요.")
            self.folder_label.setToolTip("")
            return
        self.folder_label.setText(self.workspace.name or str(self.workspace))
        self.folder_label.setToolTip(str(self.workspace))

    def change_recursive_option(self):
        if self.worker is not None:
            return
        self.refresh_files()
        if self.workspace is not None:
            self.save_workspace_settings()

    def save_workspace_settings(self):
        if self.workspace is None:
            return
        try:
            build_workspace_settings_repository().save(self.workspace, self.recursive.isChecked())
        except Exception as error:
            self.statusBar().showMessage("작업 폴더 설정 저장 실패")
            QMessageBox.warning(self, "설정 저장 실패", f"{type(error).__name__}: {error}")

    def restore_workspace_settings(self):
        try:
            settings = build_workspace_settings_repository().load()
        except Exception as error:
            self.statusBar().showMessage("작업 폴더 설정 읽기 실패")
            QMessageBox.warning(self, "설정 읽기 실패", f"{type(error).__name__}: {error}")
            return
        if settings is None:
            return

        self.recursive.blockSignals(True)
        self.recursive.setChecked(settings.recursive)
        self.recursive.blockSignals(False)
        try:
            is_valid = settings.workspace.is_dir()
        except OSError:
            is_valid = False
        if not is_valid:
            self.folder_label.setText(f"저장된 작업 폴더를 사용할 수 없습니다: {settings.workspace}")
            self.statusBar().showMessage("저장된 작업 폴더를 사용할 수 없음")
            QMessageBox.warning(
                self,
                "작업 폴더 복원 실패",
                "저장된 작업 폴더가 없거나 접근할 수 없습니다. 새 작업 폴더를 선택하세요.\n"
                f"{settings.workspace}",
            )
            return

        self.workspace = settings.workspace
        self._show_workspace_name()
        self.refresh_files()

    def refresh_files(self):
        self.file_table.setRowCount(0)
        self.file_count_label.setText("0개")
        if self.workspace is None:
            return
        try:
            if not self.workspace.is_dir():
                raise OSError("선택한 폴더가 없거나 접근할 수 없습니다.")
            entries = self.workspace.rglob("*") if self.recursive.isChecked() else self.workspace.iterdir()
            files = sorted(
                (
                    path
                    for path in entries
                    if path.is_file() and path.suffix.lower() == ".pptx" and not path.name.startswith("~")
                ),
                key=lambda path: str(path).casefold(),
            )
        except OSError as error:
            self.statusBar().showMessage("파일 목록 읽기 실패")
            QMessageBox.warning(self, "폴더 읽기 실패", str(error))
            return

        self.file_table.setRowCount(len(files))
        for row, path in enumerate(files):
            relative_folder = path.parent.relative_to(self.workspace)
            folder_text = str(relative_folder) if str(relative_folder) != "." else "현재 폴더"
            try:
                size_mb = path.stat().st_size / (1024 * 1024)
                metadata = f"{folder_text} · {size_mb:.1f} MB"
            except OSError:
                metadata = folder_text
            name_item = QTableWidgetItem(f"{path.name}\n{metadata}")
            name_item.setData(Qt.ItemDataRole.UserRole, str(path))
            name_item.setToolTip(str(path))
            self.file_table.setItem(row, 0, name_item)
            self.file_table.setRowHeight(row, 62)
        self.file_count_label.setText(f"{len(files)}개")
        self.statusBar().showMessage(f"PPTX 파일 {len(files)}개")

    def select_file(self, current_row: int, _current_column: int, _previous_row: int, _previous_column: int):
        if current_row < 0 or self.worker is not None:
            return
        item = self.file_table.item(current_row, 0)
        if item is None:
            return
        self.selected_file = Path(item.data(Qt.ItemDataRole.UserRole))
        self.selected_file_label.setText(self.selected_file.name)
        self.selected_file_label.setToolTip(str(self.selected_file))
        try:
            size_mb = self.selected_file.stat().st_size / (1024 * 1024)
            self.file_meta_label.setText(f"PPTX · {size_mb:.1f} MB")
        except OSError:
            self.file_meta_label.setText("PPTX")
        self.analyze_button.setEnabled(True)
        self.analyze_button.show()
        self.history_banner.hide()
        self._set_active_tab("files")
        self.left_stack.setCurrentWidget(self.files_left_page)
        self.show_pre_analysis()

    def start_analysis(self):
        if self.worker is not None:
            return
        path = self.selected_file
        if path is None:
            row = self.file_table.currentRow()
            if row >= 0 and self.file_table.item(row, 0) is not None:
                path = Path(self.file_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        if path is None:
            QMessageBox.information(self, "파일 선택", "검사할 PPTX 파일을 먼저 선택하세요.")
            return

        self.selected_file = path
        self.progress_file_label.setText(f"검사 중 · {path.name}")
        self.inspection_stack.setCurrentWidget(self.progress_page)
        self.statusBar().showMessage("검사 중")
        self._set_busy(True)

        self.worker = AnalysisWorker(path, self)
        self.worker.succeeded.connect(self.show_report)
        self.worker.failed.connect(self.show_analysis_error)
        self.worker.storage_failed.connect(self.show_storage_error)
        self.worker.finished.connect(self.finish_analysis)
        self.worker.start()

    def _set_busy(self, busy: bool):
        self.choose_button.setEnabled(not busy)
        self.files_tab_button.setEnabled(not busy)
        self.history_tab_button.setEnabled(not busy)
        self.recursive.setEnabled(not busy)
        self.file_table.setEnabled(not busy)
        self.file_refresh_button.setEnabled(not busy)
        self.analyze_button.setEnabled(not busy and self.selected_file is not None)

    def show_report(self, report, from_history=False, created_at=None):
        self.current_report = report
        self.viewing_history = from_history
        deck = report.deck
        source_name = Path(deck.source_path).name
        self.selected_file_label.setText(source_name)
        self.file_meta_label.setText(f"{deck.slide_count}개 슬라이드 · 실행 규칙 {len(report.executed_rules)}개")

        if from_history:
            self.history_banner.setText(f"과거 이력 결과 · 검사 시각 {_display_time(created_at)}")
            self.history_banner.show()
            self.analyze_button.hide()
            self._set_active_tab("history")
            self.left_stack.setCurrentWidget(self.history_left_page)
        else:
            self.history_banner.hide()
            self.analyze_button.show()
            self._set_active_tab("files")
            self.left_stack.setCurrentWidget(self.files_left_page)

        self.inspection_stack.setCurrentWidget(self.results_page)
        self.result_table.setRowCount(len(report.findings))

        if report.status == "not_configured":
            self.result_label.setText(
                f"{source_name} · 파일 읽기 완료 · 검사 규칙이 설정되지 않아 가독성을 판정하지 않았습니다."
            )
        elif report.findings:
            unknown_count = sum(finding.severity == "unknown" for finding in report.findings)
            suffix = f" · 판정 불가 {unknown_count}건" if unknown_count else ""
            self.result_label.setText(
                f"검사 완료 · {deck.slide_count}개 슬라이드 · 발견 항목 {len(report.findings)}건{suffix}"
            )
        else:
            self.result_label.setText(f"검사 완료 · {deck.slide_count}개 슬라이드 · 발견 항목 없음")

        limitations = tuple(
            limitation
            for limitation in deck.limitations
            if not (report.status == "analyzed" and limitation == "No rules registered")
        )
        if limitations:
            self.result_label.setText(self.result_label.text() + "\n측정 한계: " + " / ".join(limitations))

        for row, finding in enumerate(report.findings):
            values = [
                _slide_number(finding.shape_key),
                _rule_name(finding.rule_id),
                _severity_name(finding.severity),
                finding.message,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(finding.evidence)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, finding)
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.result_table.setItem(row, column, item)
            self.result_table.setCellWidget(row, 2, _severity_badge(finding.severity))

        if report.findings:
            self.result_table.selectRow(0)
            self.show_finding_details(0, 0, -1, -1)
        else:
            self.detail_title.setText("표시할 발견 항목이 없습니다.")
            self.detail_values.clear()
            self.detail_reason.setText(
                "검사 규칙 미설정 결과인지 실제 검사 완료 결과인지는 위의 검사 상태를 확인하세요."
            )

        status = "검사 규칙 미설정" if report.status == "not_configured" else "검사 완료"
        prefix = "저장된 이력 · " if from_history else ""
        self.statusBar().showMessage(prefix + status)

    def show_finding_details(self, current_row: int, *_args):
        if current_row < 0:
            return
        item = self.result_table.item(current_row, 0)
        if item is None:
            return
        finding = item.data(Qt.ItemDataRole.UserRole)
        if finding is None:
            return
        self.detail_title.setText(
            f"{_slide_number(finding.shape_key)} 슬라이드 · {_rule_name(finding.rule_id)} · "
            f"{_severity_name(finding.severity)}"
        )
        measured = _display_value(finding.measured)
        expected = _display_value(finding.expected)
        self.detail_values.setText(f"측정값   {measured}\n검사 기준   {expected}")
        self.detail_reason.setText(f"판정 이유\n{finding.evidence}")

    def show_analysis_error(self, message):
        self.error_page.description_label.setText(message)
        self.inspection_stack.setCurrentWidget(self.error_page)
        self.statusBar().showMessage("검사 실패")
        QMessageBox.warning(self, "검사 실패", message)

    def show_storage_error(self, message):
        self.statusBar().showMessage("검사 결과 저장 실패")
        QMessageBox.warning(
            self,
            "이력 저장 실패",
            "검사 결과는 화면에 표시했지만 이력에는 저장하지 못했습니다.\n" + message,
        )

    def refresh_history(self):
        try:
            stored_reports = build_report_repository().list_recent()
        except Exception as error:
            self.statusBar().showMessage("검사 이력 읽기 실패")
            QMessageBox.warning(self, "이력 읽기 실패", f"{type(error).__name__}: {error}")
            return

        current_id = None
        current_item = self.history_table.item(self.history_table.currentRow(), 0)
        if current_item is not None:
            current = current_item.data(Qt.ItemDataRole.UserRole)
            current_id = current.id if current is not None else None

        self.history_table.setRowCount(len(stored_reports))
        self.history_count_label.setText(f"{len(stored_reports)}건")
        self.clear_history_button.setEnabled(bool(stored_reports))
        status_names = {"not_configured": "규칙 미설정", "analyzed": "검사 완료"}
        selected_row = -1
        for row, stored in enumerate(stored_reports):
            report = stored.report
            status = status_names.get(report.status, report.status)
            summary = (
                f"{_display_time(stored.created_at)}\n"
                f"{Path(report.deck.source_path).name}\n"
                f"{status} · 발견 항목 {len(report.findings)}건"
            )
            item = QTableWidgetItem(summary)
            item.setData(Qt.ItemDataRole.UserRole, stored)
            item.setToolTip(report.deck.source_path)
            self.history_table.setItem(row, 0, item)
            self.history_table.setRowHeight(row, 86)
            if stored.id == current_id:
                selected_row = row

        if selected_row >= 0:
            self.history_table.selectRow(selected_row)

    def open_selected_history(self, *_args):
        row = self.history_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "이력 선택", "열어볼 검사 이력을 먼저 선택하세요.")
            return
        item = self.history_table.item(row, 0)
        if item is not None:
            self.open_history_record(item.data(Qt.ItemDataRole.UserRole))

    def select_history(self, current_row: int, *_args):
        if current_row < 0:
            return
        item = self.history_table.item(current_row, 0)
        if item is not None:
            self.open_history_record(item.data(Qt.ItemDataRole.UserRole))

    def open_history_record(self, stored):
        if stored is None:
            return
        self.show_report(stored.report, from_history=True, created_at=stored.created_at)

    def clear_history(self):
        count = self.history_table.rowCount()
        if count == 0:
            return
        answer = QMessageBox.question(
            self,
            "검사 이력 초기화",
            "저장된 모든 검사 이력을 삭제합니다.\n"
            f"현재 목록: {count}건\n"
            "작업 폴더 설정과 원본 PPTX 파일은 유지됩니다.\n\n계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            build_report_repository().clear()
        except Exception as error:
            self.statusBar().showMessage("검사 이력 초기화 실패")
            QMessageBox.warning(self, "이력 초기화 실패", f"{type(error).__name__}: {error}")
            return
        self.history_table.clearSelection()
        self.refresh_history()
        if self.viewing_history:
            self._show_empty_history_detail()
        self.statusBar().showMessage("검사 이력을 초기화했습니다")

    def _show_empty_history_detail(self):
        self.current_report = None
        self.viewing_history = False
        self.selected_file_label.setText("검사 이력을 선택하세요")
        self.file_meta_label.setText("왼쪽 목록에서 저장된 검사 결과를 선택하세요.")
        self.history_banner.hide()
        self.analyze_button.hide()
        self.result_table.setRowCount(0)
        self.detail_values.clear()
        self.detail_reason.clear()
        self.inspection_stack.setCurrentWidget(self.history_empty_page)

    def finish_analysis(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        self._set_busy(False)
        self.refresh_history()

    def closeEvent(self, event):
        if self.worker is not None:
            QMessageBox.information(self, "검사 진행 중", "검사가 끝난 뒤 창을 닫아주세요.")
            event.ignore()
            return
        super().closeEvent(event)


def _slide_number(shape_key: str) -> str:
    value = shape_key.partition(":")[0]
    return value if value.isdigit() else "-"


def _rule_name(rule_id: str) -> str:
    root = rule_id.partition(".")[0]
    return {"contrast": "명암비", "typography": "글자 크기", "layout": "배치"}.get(root, rule_id)


def _severity_name(severity: str) -> str:
    return {"warning": "문제 발견", "review": "검토 필요", "unknown": "판정 불가"}.get(severity, severity)


def _severity_badge(severity: str) -> QWidget:
    colors = {
        "warning": ("#b42318", "#fee4e2"),
        "review": ("#9a6700", "#fff1c2"),
        "unknown": ("#52606d", "#e9edf2"),
    }
    foreground, background = colors.get(severity, ("#364152", "#eef1f5"))
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(6, 4, 6, 4)
    badge = QLabel(_severity_name(severity))
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setStyleSheet(
        f"color: {foreground}; background: {background}; border-radius: 8px; "
        "font-weight: 700; padding: 2px 8px;"
    )
    layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)
    return container


def _display_value(value) -> str:
    return "확인할 수 없음" if value is None else str(value)


def _display_time(value: str | None) -> str:
    if not value:
        return "시각 정보 없음"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
