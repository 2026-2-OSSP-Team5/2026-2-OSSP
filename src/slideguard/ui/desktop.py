import sys

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SlideGuard")
        self.resize(840, 520)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(QLabel("SlideGuard | PPT 가독성 진단 도구"))
        layout.addWidget(QLabel("PowerPoint 파일(.pptx)을 선택하세요."))
        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)
        choose = QPushButton("PPTX 파일 선택")
        choose.clicked.connect(self.choose_file)
        layout.addWidget(choose)
        analyze = QPushButton("가독성 검사 사용 불가")
        analyze.setEnabled(False)
        layout.addWidget(analyze)
        layout.addStretch()
        self.setCentralWidget(container)
        self.statusBar().showMessage("PPTX 파일 선택 대기")

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "검사할 파일 선택", "", "PowerPoint (*.pptx)")
        if path:
            self.file_label.setText(path)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
