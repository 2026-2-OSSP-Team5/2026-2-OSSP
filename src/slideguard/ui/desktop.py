import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from slideguard.bootstrap import build_analyzer
from slideguard.domain.policy import AnalysisPolicy


class AnalysisWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            analyzer = build_analyzer()
            report = analyzer.execute(self.path, AnalysisPolicy())
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
        else:
            self.succeeded.emit(report)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.workspace: Path | None = None
        self.worker: AnalysisWorker | None = None

        self.setWindowTitle("SlideGuard")
        self.resize(1100, 760)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(QLabel("SlideGuard | PPT 가독성 진단 도구"))

        folder_row = QHBoxLayout()
        self.folder_label = QLabel("작업 폴더를 선택하세요.")
        self.folder_label.setWordWrap(True)
        folder_row.addWidget(self.folder_label, 1)

        choose_button = QPushButton("작업 폴더 선택")
        choose_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(choose_button)
        layout.addLayout(folder_row)

        options_row = QHBoxLayout()
        self.recursive = QCheckBox("하위 폴더 포함")
        self.recursive.toggled.connect(self.refresh_files)
        options_row.addWidget(self.recursive)
        options_row.addStretch()

        refresh_button = QPushButton("새로고침")
        refresh_button.clicked.connect(self.refresh_files)
        options_row.addWidget(refresh_button)
        layout.addLayout(options_row)

        self.file_table = QTableWidget(0, 2)
        self.file_table.setHorizontalHeaderLabels(["파일명", "폴더"])
        self.file_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.file_table)

        self.analyze_button = QPushButton("선택한 파일 검사")
        self.analyze_button.clicked.connect(self.start_analysis)
        layout.addWidget(self.analyze_button)

        self.result_label = QLabel("파일을 선택한 뒤 검사 버튼을 눌러주세요.")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["객체 위치", "판정", "문제 내용", "근거"])
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.result_table)

        self.setCentralWidget(container)
        self.statusBar().showMessage("작업 폴더 선택 대기")

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "관리할 PPTX 작업 폴더 선택",
            str(self.workspace) if self.workspace else "",
        )
        if not folder:
            return

        self.workspace = Path(folder)
        self.folder_label.setText(str(self.workspace))
        self.result_table.setRowCount(0)
        self.result_label.setText("파일을 선택한 뒤 검사 버튼을 눌러주세요.")
        self.refresh_files()

    def refresh_files(self):
        self.file_table.setRowCount(0)
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
            name_item = QTableWidgetItem(path.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.file_table.setItem(row, 0, name_item)

            relative_folder = path.parent.relative_to(self.workspace)
            self.file_table.setItem(row, 1, QTableWidgetItem(str(relative_folder)))

        self.statusBar().showMessage(f"PPTX 파일 {len(files)}개")

    def start_analysis(self):
        if self.worker is not None:
            return

        row = self.file_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "파일 선택", "검사할 PPTX 파일을 먼저 선택하세요.")
            return

        item = self.file_table.item(row, 0)
        path = Path(item.data(Qt.ItemDataRole.UserRole))

        self.result_table.setRowCount(0)
        self.result_label.setText(f"검사 중: {path.name}")
        self.statusBar().showMessage("검사 중")
        self.centralWidget().setEnabled(False)

        self.worker = AnalysisWorker(path, self)
        self.worker.succeeded.connect(self.show_report)
        self.worker.failed.connect(self.show_analysis_error)
        self.worker.finished.connect(self.finish_analysis)
        self.worker.start()

    def show_report(self, report):
        deck = report.deck

        if report.status == "not_configured":
            summary = (
                f"{Path(deck.source_path).name} | "
                f"슬라이드 {deck.slide_count}개 | "
                "파일 읽기 완료. 실행된 검사 규칙이 없어 "
                "가독성은 판정하지 않았습니다."
            )
        else:
            summary = (
                f"{Path(deck.source_path).name} | "
                f"슬라이드 {deck.slide_count}개 | "
                f"실행 규칙 {len(report.executed_rules)}개 | "
                f"발견 항목 {len(report.findings)}개"
            )

        if deck.limitations:
            summary += "\n측정 한계: " + " / ".join(deck.limitations)

        self.result_label.setText(summary)
        self.result_table.setRowCount(len(report.findings))

        severity_names = {
            "warning": "경고",
            "review": "검토 필요",
            "unknown": "판정 불가",
        }

        for row, finding in enumerate(report.findings):
            values = [
                finding.shape_key,
                severity_names.get(finding.severity, finding.severity),
                finding.message,
                finding.evidence,
            ]
            details = (
                f"규칙: {finding.rule_id}\n"
                f"측정값: {finding.measured}\n"
                f"기준: {finding.expected}\n"
                f"근거: {finding.evidence}"
            )

            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(details)
                self.result_table.setItem(row, column, cell)

        self.statusBar().showMessage(
            "파일 읽기 완료 · 검사 규칙 미설정" if report.status == "not_configured" else "검사 완료"
        )

    def show_analysis_error(self, message):
        self.result_label.setText("검사에 실패했습니다.")
        self.statusBar().showMessage("검사 실패")
        QMessageBox.warning(self, "검사 실패", message)

    def finish_analysis(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()

        self.centralWidget().setEnabled(True)

    def closeEvent(self, event):
        if self.worker is not None:
            QMessageBox.information(
                self,
                "검사 진행 중",
                "검사가 끝난 뒤 창을 닫아주세요.",
            )
            event.ignore()
            return

        super().closeEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
