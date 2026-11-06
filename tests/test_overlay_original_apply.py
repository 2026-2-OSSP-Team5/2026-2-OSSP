"""오버레이의 '원본에 적용 (백업 후 저장)' 흐름 테스트 (PowerPoint 없이 가짜 패처)."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from slideguard.adapters.powerpoint.patcher import ApplyResult
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning.bindings import TeamBindings
from slideguard.ui import overlay
from tests.test_overlay import _Collection, _proposal_report, _SettingsRepository, _Tracker


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class _OriginalPatcher:
    calls: list = []

    def __init__(self):
        self.last_result = None

    def apply_to_original(self, report, proposal_ids, backup_dir, policy):
        del policy
        _OriginalPatcher.calls.append((tuple(proposal_ids), Path(backup_dir)))
        selected = [item for item in report.proposals if item.id in proposal_ids]
        bindings = TeamBindings()
        backup = Path(backup_dir) / "source_backup.pptx"
        self.last_result = ApplyResult(
            report.deck.source_path,
            applied=[bindings.from_team_proposal(item) for item in selected],
            in_place=True,
            backup_path=str(backup),
            was_open=True,
        )
        return backup


def _controller(source, monkeypatch, tmp_path, *, saved=True):
    settings = _SettingsRepository(tmp_path)
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    return overlay.OverlayController(
        tracker=_Tracker(None),
        path_provider=lambda: source,
        state_provider=lambda: overlay.PresentationState(source, saved),
        collection_builder=lambda *_args, **_kwargs: _Collection(),
        patcher_builder=_OriginalPatcher,
    )


def _close(controller):
    controller.stop()
    controller.settings_dialog.close()
    controller.overlay.close()


def test_original_worker_reports_backup_and_applied_ids(tmp_path):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    selected = (report.proposals[0].id,)
    outcomes, failures = [], []
    worker = overlay.OriginalApplyWorker(
        report, selected, tmp_path / "backups", AnalysisPolicy(), patcher_builder=_OriginalPatcher
    )
    worker.succeeded.connect(outcomes.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert outcomes[0].applied_ids == selected
    assert outcomes[0].backup_path == tmp_path / "backups" / "source_backup.pptx"
    assert outcomes[0].was_open


def test_original_button_follows_selection(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    controller = _controller(source, monkeypatch, tmp_path)
    try:
        controller.overlay.show_report(report)
        assert not controller.overlay.apply_original_button.isEnabled()
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)
        assert controller.overlay.apply_original_button.isEnabled()
        count = len(controller.overlay.selected_proposal_ids())  # 묶음 제안은 함께 선택됨
        assert f"({count})" in controller.overlay.apply_original_button.text()
    finally:
        _close(controller)


def test_unsaved_presentation_blocks_original_apply_before_confirmation(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    warnings = []
    monkeypatch.setattr(overlay.QMessageBox, "warning", lambda _p, title, message: warnings.append(message))
    monkeypatch.setattr(overlay.QMessageBox, "question", lambda *_a: pytest.fail("확인 창 전에 막혀야 함"))
    controller = _controller(source, monkeypatch, tmp_path, saved=False)
    try:
        controller.overlay.show_report(report)
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)
        controller.apply_selected_to_original()
        assert "저장" in warnings[0] and controller._apply_thread is None
    finally:
        _close(controller)


def test_declined_confirmation_changes_nothing(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    monkeypatch.setattr(overlay.QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.No)
    _OriginalPatcher.calls = []
    controller = _controller(source, monkeypatch, tmp_path)
    try:
        controller.overlay.show_report(report)
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)
        controller.apply_selected_to_original()
        assert controller._apply_thread is None and _OriginalPatcher.calls == []
        assert source.read_bytes() == b"original bytes"
    finally:
        _close(controller)


def test_confirmed_original_apply_runs_patcher_and_marks_report_stale(application, tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"original bytes")
    report = _proposal_report(source, overlay._sha256(source))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(overlay.QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(overlay.QMessageBox, "exec", lambda self: 0)
    _OriginalPatcher.calls = []
    controller = _controller(source, monkeypatch, tmp_path)
    try:
        controller.overlay.show_report(report)
        controller.overlay.proposal_items[report.proposals[0].id].setCheckState(Qt.CheckState.Checked)
        selected = controller.overlay.selected_proposal_ids()
        controller.apply_selected_to_original()
        thread = controller._apply_thread
        assert thread is not None
        thread.wait(5000)
        for _ in range(20):
            QApplication.processEvents()
        backup_dir = tmp_path / "appdata" / "SlideGuard" / "backups"
        assert _OriginalPatcher.calls == [(selected, backup_dir)]
        assert controller.overlay.status_label.text() == "재검사 필요" or "원본 적용 완료" in (
            controller.overlay.status_label.text()
        )
        assert controller.overlay._report_stale
    finally:
        _close(controller)
