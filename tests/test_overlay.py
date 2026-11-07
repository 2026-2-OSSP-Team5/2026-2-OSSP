import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from slideguard.adapters.powerpoint.patcher import ApplyResult
from slideguard.adapters.storage.sqlite import WorkspaceSettings
from slideguard.bootstrap import build_analyzer
from slideguard.domain.models import (
    AnalysisReport,
    Box,
    ChangeProposal,
    DeckSnapshot,
    Finding,
    Shape,
    TextRun,
)
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning.bindings import TeamBindings
from slideguard.ui import desktop, overlay


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def _report(path: Path, source_sha256: str) -> AnalysisReport:
    return AnalysisReport(
        deck=DeckSnapshot(
            source_path=str(path),
            source_sha256=source_sha256,
            slide_count=1,
            width_pt=960,
            height_pt=540,
        ),
        findings=[
            Finding(
                id="typography:1:2",
                rule_id="typography.font_size",
                shape_key="1:2",
                severity="unknown",
                message="글자 크기를 판정할 수 없습니다.",
                measured=None,
                expected="14 pt 이상",
                evidence="측정 정보가 없습니다.",
            )
        ],
        executed_rules=("typography",),
        status="analyzed",
    )


def _proposal_report(path: Path, source_sha256: str) -> AnalysisReport:
    warning = Finding(
        id="typography:1:2",
        rule_id="typography",
        shape_key="1:2",
        severity="warning",
        message="글자가 작습니다.",
        measured=10.0,
        expected=14.0,
        evidence="10pt < 14pt",
    )
    review = Finding(
        id="layout.margin:1:3",
        rule_id="layout.margin",
        shape_key="1:3",
        severity="review",
        message="안전 여백을 확인하세요.",
        measured=10.0,
        expected=27.0,
        evidence="여백 확인 필요",
    )
    unknown = Finding(
        id="contrast:1:4",
        rule_id="contrast",
        shape_key="1:4",
        severity="unknown",
        message="명암비를 판정할 수 없습니다.",
        measured=None,
        expected=None,
        evidence="배경색 확인 불가",
    )
    return AnalysisReport(
        deck=DeckSnapshot(str(path), source_sha256, 1, 960, 540),
        findings=[warning, review, unknown],
        proposals=[
            ChangeProposal(
                "font.size@1:2#bundle-a",
                warning.id,
                "1:2",
                "font.size",
                10.0,
                14.0,
                0.1,
            ),
            ChangeProposal(
                "shape.height@1:2#bundle-a",
                warning.id,
                "1:2",
                "shape.height",
                40.0,
                50.0,
                0.1,
            ),
            ChangeProposal(
                "shape.position@1:3",
                review.id,
                "1:3",
                "shape.position",
                (10.0, 10.0),
                (27.0, 27.0),
                0.1,
            ),
        ],
        executed_rules=("typography", "layout", "contrast"),
        status="analyzed",
    )


def test_collapsed_overlay_keeps_state_and_shows_result_count(application, tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"deck")
    widget = overlay.OverlayWidget()
    try:
        widget.set_target(overlay.PowerPointWindow(1, QRect(0, 0, 1200, 800), "deck.pptx - PowerPoint"))
        widget.show_report(_report(path, overlay._sha256(path)))
        application.processEvents()

        assert widget.collapsed is True
        assert widget.size().width() == 58
        assert widget.size().height() == 58
        assert widget.count_badge.text() == "1"
        assert widget.count_badge.isVisible() is True
        assert widget.findings_table.rowCount() == 1
        assert widget.apply_button.isEnabled() is False
        assert widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is False
        assert widget.mask().isEmpty() is False
        assert isinstance(widget.shield_button, overlay.ShieldButton)
        assert widget.shield_button.text() == ""
    finally:
        widget.close()


def test_only_one_desktop_instance_can_hold_the_user_lock(tmp_path):
    lock_path = tmp_path / "slideguard-instance.lock"
    first = desktop._acquire_instance_lock(lock_path)
    assert first is not None
    try:
        assert desktop._acquire_instance_lock(lock_path) is None
    finally:
        first.unlock()

    next_instance = desktop._acquire_instance_lock(lock_path)
    assert next_instance is not None
    next_instance.unlock()


def test_exit_button_emits_exit_request(application):
    widget = overlay.OverlayWidget()
    requests = []
    widget.exit_requested.connect(lambda: requests.append(True))
    try:
        widget.set_collapsed(False)
        widget.exit_button.click()

        assert requests == [True]
        assert widget.exit_button.text() == "종료"
        assert widget.exit_button.objectName() == "exit"
    finally:
        widget.close()


def test_collapsing_visible_overlay_keeps_one_visible_window(application, monkeypatch):
    widget = overlay.OverlayWidget()
    target = overlay.PowerPointWindow(1, QRect(0, 0, 1200, 800), "deck.pptx - PowerPoint")
    try:
        widget.set_target(target)
        application.processEvents()

        widget.set_collapsed(False)
        application.processEvents()

        assert widget.isVisible() is True
        assert widget.collapsed is False
        assert widget.mask().isEmpty() is True
        assert widget.geometry().left() == 10
        assert widget.geometry().top() == 32
        assert widget.geometry().width() == 1180
        assert widget.geometry().height() >= 340

        geometry_calls = []
        monkeypatch.setattr(widget, "setGeometry", lambda *_args: geometry_calls.append(True))
        widget._apply_geometry()
        assert geometry_calls == []
    finally:
        widget.close()


def test_powerpoint_physical_rect_is_mapped_to_qt_monitor_coordinates():
    rect = overlay._to_qt_rect(
        (2560, 709, 4480, 1789),
        (2560, 709, 4480, 1789),
        QRect(2560, 709, 1536, 864),
    )

    assert rect == QRect(2560, 709, 1536, 864)


def test_proposal_selection_by_severity_and_bundle(application, tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"deck")
    report = _proposal_report(source, overlay._sha256(source))
    widget = overlay.OverlayWidget()
    try:
        widget.show_report(report, {report.findings[2].id: "측정값이 없어 제안을 만들지 않았습니다."})
        widget.select_severity("warning")

        assert set(widget.selected_proposal_ids()) == {
            "font.size@1:2#bundle-a",
            "shape.height@1:2#bundle-a",
        }
        assert widget.apply_button.text().endswith("(2)")

        widget.proposal_items["font.size@1:2#bundle-a"].setCheckState(Qt.CheckState.Unchecked)
        assert widget.selected_proposal_ids() == ()

        widget.select_severity("review")
        assert widget.selected_proposal_ids() == ("shape.position@1:3",)
        first_item = widget.proposals_list.item(0)
        assert widget.proposals_list.itemWidget(first_item) is None
        assert (
            widget.proposals_list.verticalScrollMode() == overlay.QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        widget.clear_proposals()
        assert widget.apply_button.isEnabled() is False

        widget.select_severity("warning")
        assert widget.apply_button.isEnabled() is True
        widget.show_detected(source)
        assert widget.manual_button.isEnabled() is False
        assert widget.apply_button.isEnabled() is False

        widget.show_report(report)
        assert widget.manual_button.isEnabled() is True
        assert widget.apply_button.isEnabled() is False
    finally:
        widget.close()


def test_current_b_finding_is_connected_to_c_proposal_planner(tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"deck")
    deck = DeckSnapshot(
        source_path=str(source),
        source_sha256=overlay._sha256(source),
        slide_count=1,
        width_pt=960,
        height_pt=540,
        shapes=(
            Shape(
                slide_number=1,
                shape_id=2,
                name="Body",
                box=Box(100, 100, 400, 100),
                text="small text",
                runs=(TextRun("small text", font_size_pt=10, color=(0, 0, 0)),),
                background=(255, 255, 255),
                supported=True,
            ),
        ),
    )
    analyzer = build_analyzer()
    report = AnalysisReport(deck=deck)
    for rule in analyzer.rules:
        report.findings.extend(rule.evaluate(deck, AnalysisPolicy()))

    report.proposals = analyzer.planner.propose(report, AnalysisPolicy())

    typography = [item for item in report.findings if item.rule_id == "typography"]
    assert any(item.severity == "warning" and item.measured == 10 for item in typography)
    proposal = next(item for item in report.proposals if item.property == "font.size")
    assert proposal.shape_key == "1:2"
    assert proposal.before == 10
    assert proposal.after == 14


def test_selection_conflicts_detect_alternatives_and_partial_bundles(tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"deck")
    report = _proposal_report(source, overlay._sha256(source))
    alternative = ChangeProposal(
        "font.size@1:2#alternative",
        report.findings[0].id,
        "1:2",
        "font.size",
        10.0,
        18.0,
        0.2,
    )
    report.proposals.append(alternative)

    conflicts = overlay._selection_conflicts(
        report,
        [report.proposals[0], alternative],
    )

    assert any("묶음" in message for message in conflicts)
    assert any("서로 다른 변경값" in message for message in conflicts)


class _SettingsRepository:
    def __init__(self, workspace: Path):
        self.settings = WorkspaceSettings(workspace, False, "presentation")

    def load(self):
        return self.settings

    def save(self, workspace, recursive, purpose=None):
        self.settings = WorkspaceSettings(workspace, recursive, purpose or self.settings.purpose)


class _Collector:
    def __init__(self):
        self.requests = []

    def request(self, path):
        self.requests.append(path)


class _Collection:
    def __init__(self):
        self.collector = _Collector()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _Tracker:
    def __init__(self, target):
        self.target = target

    def poll(self, _owned_hwnds):
        return self.target


def test_manual_and_saved_analysis_share_collection_and_stale_result_is_rejected(
    application, tmp_path, monkeypatch
):
    active = tmp_path / "active.pptx"
    other = tmp_path / "other.pptx"
    active.write_bytes(b"active")
    other.write_bytes(b"other")
    settings = _SettingsRepository(tmp_path)
    collection = _Collection()
    callbacks = {}

    def build_collection(_folder, on_snapshot, **kwargs):
        callbacks["snapshot"] = on_snapshot
        callbacks.update(kwargs)
        return collection

    current = {"path": active}
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    tracker = _Tracker(overlay.PowerPointWindow(1, QRect(0, 0, 1200, 800), "active.pptx - PowerPoint"))
    controller = overlay.OverlayController(
        tracker=tracker,
        path_provider=lambda: current["path"],
        collection_builder=build_collection,
    )
    try:
        controller.poll_powerpoint()
        controller.request_manual()
        assert collection.started is True
        assert collection.collector.requests == [active]
        assert callbacks["on_detected"] is not None
        assert controller.overlay.manual_button.isEnabled() is False

        current["path"] = other
        controller._on_completed(_report(active, overlay._sha256(active)))
        assert controller.overlay._report is None

        current["path"] = active
        controller._on_completed(_report(active, overlay._sha256(active)))
        application.processEvents()
        assert controller.overlay._report is not None
        assert controller.overlay.findings_table.rowCount() == 1
        assert controller.overlay.manual_button.isEnabled() is True
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()


def test_changed_file_revision_cannot_replace_current_result(application, tmp_path, monkeypatch):
    active = tmp_path / "active.pptx"
    active.write_bytes(b"before")
    settings = _SettingsRepository(tmp_path)
    collection = _Collection()
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    controller = overlay.OverlayController(
        tracker=_Tracker(None),
        path_provider=lambda: active,
        collection_builder=lambda *_args, **_kwargs: collection,
    )
    try:
        report = _report(active, overlay._sha256(active))
        active.write_bytes(b"after")
        controller._on_completed(report)
        application.processEvents()
        assert controller.overlay._report is None
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()


def test_settings_dialog_persists_workspace_recursion_and_supported_purpose(
    application, tmp_path, monkeypatch
):
    workspace = tmp_path / "presentations"
    workspace.mkdir()
    repository = _SettingsRepository(workspace)
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: repository)
    dialog = overlay.SettingsDialog()
    saved = []
    dialog.settings_saved.connect(lambda: saved.append(True))
    try:
        dialog.folder_edit.setText(str(workspace))
        dialog.recursive.setChecked(True)
        dialog.purpose.setCurrentIndex(dialog.purpose.findData("lecture"))

        dialog.save()
        application.processEvents()

        assert repository.load() == WorkspaceSettings(workspace, True, "lecture")
        assert saved == [True]
    finally:
        dialog.close()


def test_settings_dialog_stays_above_overlay_and_tracker_does_not_reshow_visible_overlay(
    application, tmp_path, monkeypatch
):
    workspace = tmp_path / "presentations"
    workspace.mkdir()
    repository = _SettingsRepository(workspace)
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: repository)
    widget = overlay.OverlayWidget()
    dialog = overlay.SettingsDialog(widget)
    target = overlay.PowerPointWindow(1, QRect(0, 0, 1200, 800), "deck.pptx - PowerPoint")
    try:
        assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

        widget.set_target(target)
        application.processEvents()
        show_calls = []
        geometry_calls = []
        monkeypatch.setattr(widget, "show", lambda: show_calls.append(True))
        monkeypatch.setattr(widget, "setGeometry", lambda *_args: geometry_calls.append(True))

        widget.set_target(target)

        assert show_calls == []
        assert geometry_calls == []
    finally:
        dialog.close()
        widget.close()


class _SuccessfulPatcher:
    def __init__(self):
        self.last_result = None

    def apply_to_copy(self, report, proposal_ids, output, policy):
        del policy
        shutil.copy2(report.deck.source_path, output)
        selected = [item for item in report.proposals if item.id in proposal_ids]
        bindings = TeamBindings()
        self.last_result = ApplyResult(
            str(output),
            applied=[bindings.from_team_proposal(item) for item in selected],
        )
        return output


def test_copy_worker_applies_only_checked_proposals_and_preserves_original(tmp_path):
    source = tmp_path / "source.pptx"
    output = tmp_path / "copy.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    selected = (report.proposals[2].id,)
    outcomes = []
    failures = []
    worker = overlay.CopyApplyWorker(
        report,
        selected,
        output,
        AnalysisPolicy(),
        patcher_builder=_SuccessfulPatcher,
        preview_renderer=lambda _path, _slides: {},
    )
    worker.succeeded.connect(outcomes.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert len(outcomes) == 1
    assert outcomes[0].applied_ids == selected
    assert outcomes[0].slide_changes == {1: (report.proposals[2],)}
    assert source.read_bytes() == b"original bytes"
    assert output.read_bytes() == b"original bytes"


def test_unsaved_active_presentation_blocks_copy_apply(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    settings = _SettingsRepository(tmp_path)
    collection = _Collection()
    warnings = []
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    monkeypatch.setattr(
        overlay.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        overlay.QFileDialog,
        "getSaveFileName",
        lambda *_args: pytest.fail("save dialog must not open for unsaved content"),
    )
    controller = overlay.OverlayController(
        tracker=_Tracker(None),
        path_provider=lambda: source,
        state_provider=lambda: overlay.PresentationState(source, False),
        collection_builder=lambda *_args, **_kwargs: collection,
    )
    try:
        controller.overlay.show_report(report)
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)

        controller.apply_selected()

        assert len(warnings) == 1
        assert "저장" in warnings[0][1]
        assert controller._apply_thread is None
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()


def test_changed_saved_file_marks_report_stale_and_reenables_manual_check(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    settings = _SettingsRepository(tmp_path)
    collection = _Collection()
    warnings = []
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    monkeypatch.setattr(
        overlay.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        overlay.QFileDialog,
        "getSaveFileName",
        lambda *_args: pytest.fail("save dialog must not open for a stale report"),
    )
    controller = overlay.OverlayController(
        tracker=_Tracker(None),
        path_provider=lambda: source,
        state_provider=lambda: overlay.PresentationState(source, True),
        collection_builder=lambda *_args, **_kwargs: collection,
    )
    try:
        controller.overlay.show_report(report)
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)
        assert controller.overlay.apply_button.isEnabled() is True
        source.write_bytes(b"saved after analysis")

        controller.apply_selected()

        assert len(warnings) == 1
        assert "재검사" in warnings[0][0]
        assert controller.overlay.status_label.text() == "재검사 필요"
        assert controller.overlay.manual_button.isEnabled() is True
        assert controller.overlay.apply_button.isEnabled() is False
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()
