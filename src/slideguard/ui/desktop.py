import sys
from pathlib import Path

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.workspace: Path | None = None

        self.setWindowTitle("SlideGuard")
        self.resize(960, 600)

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
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.file_table)

        layout.addWidget(QLabel("검사 기능과 이력 저장은 이후 단계에서 연결합니다."))
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
                    if path.is_file()
                    and path.suffix.lower() == ".pptx"
                    and not path.name.startswith(("~$", "~"))
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
            self.file_table.setItem(row, 0, QTableWidgetItem(path.name))
            self.file_table.setItem(row, 1, QTableWidgetItem(str(relative_folder)))

        self.statusBar().showMessage(f"PPTX 파일 {len(files)}개")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
