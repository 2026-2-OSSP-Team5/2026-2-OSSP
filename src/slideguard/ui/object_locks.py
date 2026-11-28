"""Choose locked objects through PowerPoint selection and descriptive list entries."""

from pathlib import Path
from zipfile import BadZipFile

from pptx.exc import PackageNotFoundError
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.adapters.powerpoint.selection import reveal_object, selected_objects


class ObjectLocksEditor(QWidget):
    def __init__(self, parent=None, *, selection_provider=selected_objects, revealer=reveal_object):
        super().__init__(parent)
        self.selection_provider = selection_provider
        self.revealer = revealer
        self.document: Path | None = None
        self._keys: set[str] = set()
        self._shapes = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.add_button = QPushButton("PPT에서 선택한 객체 추가")
        self.add_button.clicked.connect(self.add_selected)
        actions.addWidget(self.add_button)
        self.reveal_button = QPushButton("PPT에서 확인")
        self.reveal_button.clicked.connect(self.reveal_selected)
        actions.addWidget(self.reveal_button)
        self.remove_button = QPushButton("고정 해제")
        self.remove_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)
        self.objects = QListWidget()
        self.objects.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.objects.setMinimumHeight(100)
        self.objects.setMaximumHeight(170)
        self.objects.itemSelectionChanged.connect(self._update_actions)
        self.objects.itemDoubleClicked.connect(lambda _item: self.reveal_selected())
        layout.addWidget(self.objects)
        self.status = QLabel(objectName="muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._update_actions()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._keys)

    def set_document(self, document: Path | None, keys):
        self.document = document.resolve() if document is not None else None
        self._shapes = {}
        if self.document is not None:
            try:
                snapshot = OoxmlDeckReader().read(self.document)
                self._shapes = {shape.key: shape for shape in snapshot.shapes}
            except (OSError, ValueError, BadZipFile, PackageNotFoundError):
                pass  # Saved locks can still be removed if the document is unavailable.
        self.setEnabled(self.document is not None)
        self.set_keys(keys)

    def set_keys(self, keys):
        self._keys = set(keys)
        self.objects.clear()
        for key in sorted(self._keys, key=lambda value: tuple(map(int, value.split(":")))):
            shape = self._shapes.get(key)
            number = key.split(":", 1)[0]
            text = " ".join(shape.text.split()) if shape is not None else ""
            name = shape.name if shape is not None else "저장된 고정 객체"
            label = f"{number}번 슬라이드 · {name}"
            if text:
                label += f"\n{text[:80]}{'…' if len(text) > 80 else ''}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(text or name)
            self.objects.addItem(item)
        self.status.setText(
            f"{len(self._keys)}개 객체가 고정됩니다."
            if self._keys
            else "PowerPoint에서 고정할 객체를 선택한 뒤 추가하세요."
        )
        self._update_actions()

    def add_selected(self):
        try:
            path, shapes = self.selection_provider()
            if self.document is None or Path(path).resolve() != self.document:
                raise ValueError(
                    f"PowerPoint에서 {self.document.name if self.document else '설정할 파일'}을 선택하세요."
                )
            if not shapes:
                raise ValueError("PowerPoint에서 고정할 객체를 선택하세요.")
            self._shapes.update((shape.key, shape) for shape in shapes)
            self.set_keys(self._keys | {shape.key for shape in shapes})
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def remove_selected(self):
        keys = {item.data(Qt.ItemDataRole.UserRole) for item in self.objects.selectedItems()}
        self.set_keys(self._keys - keys)

    def reveal_selected(self):
        items = self.objects.selectedItems()
        if self.document is None or len(items) != 1:
            return
        try:
            self.revealer(self.document, items[0].data(Qt.ItemDataRole.UserRole))
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def _update_actions(self):
        count = len(self.objects.selectedItems())
        self.reveal_button.setEnabled(count == 1)
        self.remove_button.setEnabled(count > 0)
