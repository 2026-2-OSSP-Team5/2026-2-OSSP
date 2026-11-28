from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QGuiApplication, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from slideguard.adapters.storage.element_groups import SqliteElementGroupRepository
from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.domain.policy import AnalysisPolicy
from slideguard.ui import overlay
from slideguard.ui.element_groups import ElementGroupsDialog
from slideguard.ui.slide_lens import BadgeHandle, LensMark, LensOption, build_marks
from tests.test_element_groups import report_with_group


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_card_separates_minimum_recommended_and_local_group_actions(application):
    report = report_with_group()
    mark = next(m for m in build_marks(report, 2) if m.key == "2:9")
    handle = BadgeHandle()
    try:
        handle.set_mark(mark, QRect(100, 100, 300, 26))
        handle.expand()
        assert [o.proposal_ids for o in handle.visible_options()] == [("font.size@2:9",)]
        rect = handle._layout()["variant_tabs"][1][2]
        QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        assert handle.variant == "recommended"
        assert [o.proposal_ids for o in handle.visible_options()] == [("font.size@2:9~rec",)]
        rect = handle._layout()["scope_tabs"][1][2]
        QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        assert handle.scope == "group"
        [option] = handle.visible_options()
        assert option.proposal_ids == ("font.size@1:9~rec", "font.size@2:9~rec")
        calls = []
        handle.toggled.connect(lambda ids, checked: calls.append((ids, checked)))
        QTest.mouseClick(
            handle, Qt.MouseButton.LeftButton, pos=handle._layout()["rows"][0].center().toPoint()
        )
        assert calls == [(option.proposal_ids, True)]
        requests = []
        handle.group_requested.connect(requests.append)
        QTest.mouseClick(
            handle, Qt.MouseButton.LeftButton, pos=handle._layout()["group_action"].center().toPoint()
        )
        assert requests == ["2:9"]
    finally:
        handle.close()


def test_other_slides_problems_remain_accessible_without_flagging_clean_member_as_a_problem():
    report = report_with_group()
    [mark] = build_marks(report, 3)
    assert mark.kind == "group" and mark.group_count == 3
    assert "글자 크기 · 2건" in mark.group_message
    assert all(option.scope == "group" for option in mark.options)


def test_long_proposal_card_stays_on_screen_and_scrolls_without_overlapping_tabs(application):
    options = tuple(LensOption(str(n), (str(n),), f"수정안 {n}") for n in range(40))
    handle = BadgeHandle()
    try:
        handle.set_mark(
            LensMark("1:9", None, "review", "글자", "", options=options), QRect(100, 100, 300, 26)
        )
        handle.expand()
        assert handle.height() <= QGuiApplication.primaryScreen().availableGeometry().height() - 40
        assert handle._layout()["max_scroll"] > 0
        position = handle._layout()["body_clip"].center()
        event = QWheelEvent(
            position,
            position + QPointF(handle.pos()),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        application.sendEvent(handle, event)
        assert handle._scroll > 0
        tab = handle._layout()["variant_tabs"][0][2]
        assert handle._row_at(tab.center()) == -1
        handle._scroll = handle._layout()["max_scroll"]
        layout = handle._layout()
        assert layout["body_clip"].contains(layout["group_action"].center())
    finally:
        handle.close()


def test_panel_shows_one_group_action_per_variant_and_keeps_counterparts_exclusive(application):
    panel = overlay.OverlayWidget()
    try:
        panel.show_report(report_with_group())
        assert panel.findings_table.rowCount() == 1
        assert panel.findings_table.item(0, 0).text() == "그룹 3쪽"
        assert "구성원 보기" in panel.detail_evidence.text()
        visible = [item for item, _ in panel.batch_items if not item.isHidden()]
        assert len(visible) == 1 and "그룹 3쪽 중 2쪽" in visible[0].text()
        assert all(item.isHidden() for item in panel.proposal_items.values())
        visible[0].setCheckState(Qt.CheckState.Checked)
        assert panel.selected_proposal_ids() == ("font.size@1:9", "font.size@2:9")
        panel.proposal_variant.setCurrentIndex(1)
        [recommended] = [item for item, _ in panel.batch_items if not item.isHidden()]
        recommended.setCheckState(Qt.CheckState.Checked)
        assert panel.selected_proposal_ids() == ("font.size@1:9~rec", "font.size@2:9~rec")
    finally:
        panel.close()


def test_dialog_edits_membership_and_uses_selected_reference_without_applying(application):
    report = report_with_group()
    saves, aligns = [], []
    dialog = ElementGroupsDialog(
        report,
        shape_key="2:9",
        on_save=saves.append,
        on_align=lambda g, ref: aligns.append((g, ref)) or (1, []),
    )
    try:
        assert dialog.member_keys() == ("1:9", "2:9", "3:9")
        assert "글자 크기 · 2건" in dialog.problems.toPlainText()
        dialog.objects.item(2, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.name_edit.setText("수동 쪽 번호")
        dialog.reference.setCurrentIndex(dialog.reference.findData("2:9"))
        dialog.make_alignment()
        assert saves[-1].title == "수동 쪽 번호" and saves[-1].members == ("1:9", "2:9")
        assert aligns[-1][1] == "2:9" and "수정안 1개" in dialog.status.text()
        dialog.objects.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.objects.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        assert dialog.save_group() is None and "하나 이상" in dialog.status.text()
    finally:
        dialog.close()


def test_dialog_adds_powerpoint_selection_and_rejects_another_document(application):
    report = report_with_group()
    dialog = ElementGroupsDialog(
        report, selection_provider=lambda: (report.deck.source_path, report.deck.shapes[:1])
    )
    try:
        dialog.objects.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.add_selected()
        assert "1:9" in dialog.member_keys()
        dialog.selection_provider = lambda: ("other.pptx", report.deck.shapes[:1])
        before = dialog.member_keys()
        dialog.add_selected()
        assert dialog.member_keys() == before and "이 그룹" in dialog.status.text()
    finally:
        dialog.close()


def test_controller_persists_edits_generates_alignment_and_rejects_stale_edits(
    application, tmp_path, monkeypatch
):
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"presentation")
    report = report_with_group()
    report.deck = replace(
        report.deck, source_path=str(path), source_sha256=sha256(path.read_bytes()).hexdigest()
    )
    group_repository = SqliteElementGroupRepository(tmp_path / "groups.sqlite3")
    report_repository = SqliteReportRepository(tmp_path / "reports.sqlite3")
    monkeypatch.setattr(
        overlay, "build_workspace_settings_repository", lambda: SimpleNamespace(load=lambda: None)
    )
    monkeypatch.setattr(overlay, "build_element_group_repository", lambda: group_repository)
    monkeypatch.setattr(overlay, "build_report_repository", lambda: report_repository)
    monkeypatch.setattr(overlay, "build_analysis_policy", lambda _p: AnalysisPolicy())
    monkeypatch.setattr(overlay.startup, "is_enabled", lambda: False)
    controller = overlay.OverlayController(path_provider=lambda: path)
    controller.timer.stop()
    try:
        controller.overlay.show_report(report)
        [group] = report.element_groups
        controller._save_element_group(report, replace(group, members=("1:9", "2:9")))
        assert group_repository.load(report.deck)[0].excluded == ("3:9",)
        count, skipped = controller._align_element_group(report, report.element_groups[0], "1:9")
        assert count == 1 and not skipped
        [proposal] = [p for p in report.proposals if "#align:" in p.id]
        assert proposal.shape_key == "2:9" and proposal.after == (870, 480)
        assert any(f.rule_id == "group.alignment" for f in report.findings)
        saved = report_repository.list_recent()[0].report
        assert saved.element_groups[0].members == ("1:9", "2:9")
        path.write_bytes(b"changed")
        with pytest.raises(ValueError, match="파일이 변경"):
            controller._save_element_group(report, group)
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()
        controller.lens.close()
