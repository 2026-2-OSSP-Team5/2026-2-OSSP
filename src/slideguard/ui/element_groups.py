"""Review membership and align manually inserted annotations across slides."""

import sqlite3
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from slideguard.adapters.powerpoint.selection import reveal_object, selected_objects
from slideguard.domain.models import ElementGroup
from slideguard.planning.bindings import is_recommended
from slideguard.planning.element_groups import eligible_shapes, group_problem_text
from slideguard.ui.slide_lens import rule_title
from slideguard.ui.user_text import change_text


class ElementGroupsDialog(QDialog):
    def __init__(
        self,
        report,
        parent=None,
        *,
        shape_key=None,
        on_save=None,
        on_align=None,
        selection_provider=selected_objects,
        revealer=reveal_object,
    ):
        super().__init__(parent)
        self.report = report
        self.on_save = on_save
        self.on_align = on_align
        self.selection_provider = selection_provider
        self.revealer = revealer
        self.shapes = eligible_shapes(report.deck)
        self._loading = False
        self._current = None
        self.setWindowTitle("같은 요소 그룹 · 구성원과 위치")
        self.resize(820, 690)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("주석·쪽 번호처럼 같은 역할인 요소를 묶으세요. 체크한 요소만 그룹 전체 수정에 포함됩니다.")
        )
        self.groups = QComboBox()
        for group in report.element_groups:
            state = "사용자 확인" if group.manual else "자동 추정"
            self.groups.addItem(f"{group.title} · {len(group.members)}개 슬라이드 · {state}", group)
        self.groups.addItem("새 그룹 만들기", None)
        self.groups.currentIndexChanged.connect(self._load_group)
        layout.addWidget(self.groups)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("그룹 이름 (예: 출처 주석, 쪽 번호)")
        layout.addWidget(self.name_edit)
        self.search = QLineEdit()
        self.search.setPlaceholderText("슬라이드 번호·요소 이름·내용 검색")
        self.search.textChanged.connect(self._filter_objects)
        layout.addWidget(self.search)
        self.objects = QTableWidget(0, 4)
        self.objects.setHorizontalHeaderLabels(["그룹 포함", "슬라이드 · 요소", "내용", "위치 (pt)"])
        self.objects.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.objects.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.objects.horizontalHeader().setStretchLastSection(True)
        self.objects.setColumnWidth(1, 200)
        self.objects.setColumnWidth(2, 320)
        self.objects.itemChanged.connect(self._membership_changed)
        self.objects.itemSelectionChanged.connect(self._show_local_problems)
        self.objects.itemDoubleClicked.connect(lambda _item: self.reveal_selected())
        for key, shape in sorted(
            self.shapes.items(), key=lambda item: (item[1].slide_number, item[1].shape_id)
        ):
            row = self.objects.rowCount()
            self.objects.insertRow(row)
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, key)
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.objects.setItem(row, 0, check)
            self.objects.setItem(row, 1, QTableWidgetItem(f"{shape.slide_number}쪽 · {shape.name}"))
            self.objects.setItem(row, 2, QTableWidgetItem(" ".join(shape.text.split())))
            self.objects.setItem(row, 3, QTableWidgetItem(f"왼쪽 {shape.box.x:g} · 위 {shape.box.y:g}"))
        layout.addWidget(self.objects, 1)
        actions = QHBoxLayout()
        add = QPushButton("PPT에서 선택한 요소 포함")
        add.clicked.connect(self.add_selected)
        remove = QPushButton("선택 요소 그룹에서 제외")
        remove.clicked.connect(self.remove_selected)
        reveal = QPushButton("PPT에서 확인")
        reveal.clicked.connect(self.reveal_selected)
        for button in (add, remove, reveal):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.problems = QTextBrowser()
        self.problems.setMaximumHeight(100)
        layout.addWidget(self.problems)
        self.local_problems = QTextBrowser()
        self.local_problems.setMaximumHeight(130)
        layout.addWidget(self.local_problems)
        align_row = QHBoxLayout()
        align_row.addWidget(QLabel("위치 기준 요소"))
        self.reference = QComboBox()
        align_row.addWidget(self.reference, 1)
        self.align_button = QPushButton("이 위치에 맞추는 수정안 만들기")
        self.align_button.clicked.connect(self.make_alignment)
        align_row.addWidget(self.align_button)
        layout.addLayout(align_row)
        self.status = QLabel("위치 맞춤은 수정안을 만든 뒤 선택해서 적용합니다.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        self.save_button = QPushButton("그룹 저장")
        self.save_button.clicked.connect(self.save_group)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        footer.addWidget(self.save_button)
        footer.addWidget(close)
        layout.addLayout(footer)
        index = next(
            (
                i
                for i in range(self.groups.count())
                if self.groups.itemData(i) and shape_key in self.groups.itemData(i).members
            ),
            self.groups.count() - 1,
        )
        self._seed = shape_key
        self.groups.setCurrentIndex(index)
        self._load_group(index)

    def _load_group(self, index):
        if not hasattr(self, "reference"):
            return
        group = self.groups.itemData(index)
        self._current = group or ElementGroup(f"custom:{uuid4().hex}", "새 요소 그룹", ())
        self.name_edit.setText(self._current.title)
        keys = set(group.members if group else (getattr(self, "_seed", None),))
        self._loading = True
        for row in range(self.objects.rowCount()):
            item = self.objects.item(row, 0)
            item.setCheckState(
                Qt.CheckState.Checked
                if item.data(Qt.ItemDataRole.UserRole) in keys
                else Qt.CheckState.Unchecked
            )
        self._loading = False
        self._membership_changed()
        selected = getattr(self, "_seed", None)
        if selected not in keys:
            selected = next((key for key in self.member_keys()), None)
        for row in range(self.objects.rowCount()):
            if self.objects.item(row, 0).data(Qt.ItemDataRole.UserRole) == selected:
                self.objects.setCurrentCell(row, 1)
                break

    def member_keys(self):
        return tuple(
            self.objects.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for row in range(self.objects.rowCount())
            if self.objects.item(row, 0).checkState() == Qt.CheckState.Checked
        )

    def _filter_objects(self, text):
        text = text.casefold().strip()
        for row in range(self.objects.rowCount()):
            label = " ".join(self.objects.item(row, col).text() for col in (1, 2))
            self.objects.setRowHidden(row, text not in label.casefold())

    def _membership_changed(self, _item=None):
        if self._loading or not hasattr(self, "reference"):
            return
        current = self.reference.currentData()
        self.reference.clear()
        for key in self.member_keys():
            shape = self.shapes[key]
            self.reference.addItem(
                f"{shape.slide_number}쪽 · {shape.name} · 왼쪽 {shape.box.x:g}, 위 {shape.box.y:g}", key
            )
        index = self.reference.findData(current)
        if index >= 0:
            self.reference.setCurrentIndex(index)
        group = ElementGroup("", "", self.member_keys())
        counts = group_problem_text(self.report, group)
        labels = {f.rule_id: rule_title(f) for f in self.report.findings}
        self.problems.setPlainText(
            "그룹 전체에서 발견한 문제\n"
            + (
                "\n".join(f"{labels[rule]} · {count}건" for rule, count in counts.items())
                or "검사에서 발견한 문제가 없습니다. 위치 맞춤은 별도로 만들 수 있습니다."
            )
        )
        self.align_button.setEnabled(len(self.member_keys()) >= 2)

    def _show_local_problems(self):
        rows = self.objects.selectionModel().selectedRows()
        if not rows:
            return
        key = self.objects.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        findings = [f.message for f in self.report.findings if f.shape_key == key]
        lines = ["선택 요소의 문제: " + (" / ".join(dict.fromkeys(findings)) or "발견한 문제 없음")]
        for recommended, title in ((False, "최소 수정"), (True, "권장 수정")):
            changes = [
                change_text(p)
                for p in self.report.proposals
                if p.shape_key == key and is_recommended(p.id) == recommended
            ]
            lines.append(title + ": " + (" / ".join(changes) or "제안 없음"))
        self.local_problems.setPlainText("\n".join(lines))

    def add_selected(self):
        try:
            path, shapes = self.selection_provider()
            if Path(path).resolve() != Path(self.report.deck.source_path).resolve():
                raise ValueError("이 그룹을 편집 중인 PowerPoint 문서에서 요소를 선택하세요.")
            keys = {s.key for s in shapes}
            if not keys or keys - self.shapes.keys():
                raise ValueError("검사 결과에 있는 글 요소를 선택하세요. 새 요소는 저장 후 다시 검사하세요.")
            for row in range(self.objects.rowCount()):
                item = self.objects.item(row, 0)
                if item.data(Qt.ItemDataRole.UserRole) in keys:
                    item.setCheckState(Qt.CheckState.Checked)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def remove_selected(self):
        for row in self.objects.selectionModel().selectedRows():
            self.objects.item(row.row(), 0).setCheckState(Qt.CheckState.Unchecked)

    def reveal_selected(self):
        rows = self.objects.selectionModel().selectedRows()
        if not rows:
            return
        try:
            self.revealer(
                Path(self.report.deck.source_path),
                self.objects.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole),
            )
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def _group(self):
        keys = self.member_keys()
        if not keys:
            raise ValueError("그룹에 포함할 요소를 하나 이상 선택하세요.")
        if len({self.shapes[k].slide_number for k in keys}) != len(keys):
            raise ValueError("한 슬라이드에서는 같은 역할의 요소 하나만 포함하세요.")
        return ElementGroup(
            self._current.id, self.name_edit.text().strip() or "요소 그룹", keys, self._current.excluded, True
        )

    def save_group(self):
        try:
            group = self._group()
            if self.on_save:
                self.on_save(group)
            self._current = group
            index = self.groups.currentIndex()
            self.groups.setItemData(index, group)
            self.groups.setItemText(index, f"{group.title} · {len(group.members)}개 슬라이드")
            if self.groups.itemData(self.groups.count() - 1) is not None:
                self.groups.addItem("새 그룹 만들기", None)
            self.status.setText("그룹 구성을 저장했습니다. 제외한 요소는 일괄 수정에 포함되지 않습니다.")
            return group
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(str(error))
            return None

    def make_alignment(self):
        group = self.save_group()
        if group is None or self.on_align is None:
            return
        try:
            count, skipped = self.on_align(group, self.reference.currentData())
            self._membership_changed()
            self._show_local_problems()
            self.status.setText(
                f"위치 맞춤 수정안 {count}개를 만들었습니다. 최소 수정 목록에서 확인·적용하세요."
                + ("\n" + " / ".join(skipped) if skipped else "")
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(str(error))
