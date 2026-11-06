import sqlite3

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from slideguard import bootstrap
from slideguard.adapters.storage.sqlite import (
    SqliteWorkspaceSettingsRepository,
    StoredReport,
    WorkspaceSettings,
)
from slideguard.domain.models import AnalysisReport, DeckSnapshot, Finding
from slideguard.domain.policy import Purpose
from slideguard.ui import desktop


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class FakeReportRepository:
    def __init__(self, reports=None):
        self.reports = reports or []
        self.clear_calls = 0

    def list_recent(self):
        return self.reports

    def clear(self):
        self.clear_calls += 1
        self.reports.clear()


class FakeSettingsRepository:
    def __init__(self, settings=None):
        self.settings = settings
        self.saved = []

    def load(self):
        return self.settings

    def save(self, workspace, recursive):
        self.saved.append((workspace, recursive))


def test_settings_save_and_new_instance_restore(tmp_path):
    database_path = tmp_path / "settings.sqlite3"
    workspace = tmp_path / "presentations"
    repository = SqliteWorkspaceSettingsRepository(database_path)

    repository.save(workspace, recursive=True)
    restored = SqliteWorkspaceSettingsRepository(database_path).load()

    assert restored == WorkspaceSettings(workspace=workspace, recursive=True)


def test_option_change_overwrites_previous_value(tmp_path):
    database_path = tmp_path / "settings.sqlite3"
    workspace = tmp_path / "presentations"
    repository = SqliteWorkspaceSettingsRepository(database_path)
    repository.save(workspace, recursive=False)

    repository.save(workspace, recursive=True)

    assert SqliteWorkspaceSettingsRepository(database_path).load().recursive is True


def test_analysis_purpose_is_saved_and_preserved_by_workspace_only_updates(tmp_path):
    database_path = tmp_path / "settings.sqlite3"
    first = tmp_path / "first"
    second = tmp_path / "second"
    repository = SqliteWorkspaceSettingsRepository(database_path)
    repository.save(first, recursive=False, purpose="report")

    repository.save(second, recursive=True)

    restored = SqliteWorkspaceSettingsRepository(database_path).load()
    assert restored == WorkspaceSettings(second, True, "report")


def test_window_restores_workspace_and_lists_files_without_analysis(tmp_path, monkeypatch, application):
    workspace = tmp_path / "presentations"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (workspace / "root.pptx").touch()
    (nested / "nested.pptx").touch()
    settings_repository = FakeSettingsRepository(WorkspaceSettings(workspace, True))
    analyzer_built = False

    def build_analyzer_spy():
        nonlocal analyzer_built
        analyzer_built = True

    monkeypatch.setattr(desktop, "build_workspace_settings_repository", lambda: settings_repository)
    monkeypatch.setattr(desktop, "build_report_repository", FakeReportRepository)
    monkeypatch.setattr(desktop, "build_analyzer", build_analyzer_spy)

    window = desktop.MainWindow()
    try:
        assert window.workspace == workspace
        assert window.recursive.isChecked() is True
        assert window.file_table.rowCount() == 2
        assert analyzer_built is False
        assert window.worker is None
    finally:
        window.close()


def test_window_saves_folder_and_option_changes(tmp_path, monkeypatch, application):
    workspace = tmp_path / "presentations"
    workspace.mkdir()
    settings_repository = FakeSettingsRepository()
    monkeypatch.setattr(desktop, "build_workspace_settings_repository", lambda: settings_repository)
    monkeypatch.setattr(desktop, "build_report_repository", FakeReportRepository)
    monkeypatch.setattr(desktop.QFileDialog, "getExistingDirectory", lambda *_args: str(workspace))

    window = desktop.MainWindow()
    try:
        window.choose_folder()
        window.recursive.setChecked(True)

        assert settings_repository.saved == [(workspace, False), (workspace, True)]
    finally:
        window.close()


def test_invalid_saved_workspace_is_reported_without_closing(tmp_path, monkeypatch, application):
    missing_workspace = tmp_path / "missing"
    replacement_workspace = tmp_path / "replacement"
    replacement_workspace.mkdir()
    settings_repository = FakeSettingsRepository(WorkspaceSettings(missing_workspace, False))
    warnings = []
    monkeypatch.setattr(desktop, "build_workspace_settings_repository", lambda: settings_repository)
    monkeypatch.setattr(desktop, "build_report_repository", FakeReportRepository)
    monkeypatch.setattr(
        desktop.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(replacement_workspace),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )

    window = desktop.MainWindow()
    try:
        assert window.workspace is None
        assert window.file_table.rowCount() == 0
        assert str(missing_workspace) in window.folder_label.text()
        assert warnings

        window.choose_folder()

        assert window.workspace == replacement_workspace
        assert settings_repository.saved == [(replacement_workspace, False)]
    finally:
        window.close()


def test_saved_analysis_opens_without_reading_original_pptx(tmp_path, monkeypatch, application):
    missing_source = tmp_path / "deleted.pptx"
    report = AnalysisReport(
        deck=DeckSnapshot(
            source_path=str(missing_source),
            source_sha256="stored-sha",
            slide_count=1,
            width_pt=960.0,
            height_pt=540.0,
        ),
        findings=[
            Finding(
                id="contrast:1:1",
                rule_id="contrast",
                shape_key="1:1",
                severity="unknown",
                message="측정할 수 없습니다",
                measured=None,
                expected=None,
                evidence="렌더링 정보 없음",
            )
        ],
        executed_rules=("contrast", "typography", "layout"),
        status="analyzed",
    )
    history = FakeReportRepository([StoredReport(1, "2026-11-06T00:00:00+00:00", report)])
    monkeypatch.setattr(desktop, "build_workspace_settings_repository", FakeSettingsRepository)
    monkeypatch.setattr(desktop, "build_report_repository", lambda: history)
    monkeypatch.setattr(
        desktop,
        "build_analyzer",
        lambda: pytest.fail("opening history must not rebuild or execute the analyzer"),
    )

    window = desktop.MainWindow()
    try:
        assert not missing_source.exists()
        assert window.history_table.rowCount() == 1

        window.history_table.selectRow(0)
        window.open_selected_history()

        assert window.result_table.rowCount() == 1
        assert window.result_table.item(0, 0).text() == "1"
        assert "deleted.pptx" in window.selected_file_label.text()
    finally:
        window.close()


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


class ControlledWorker:
    def __init__(self, path, parent=None):
        self.path = path
        self.parent = parent
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()
        self.storage_failed = FakeSignal()
        self.finished = FakeSignal()
        self.started = False

    def start(self):
        self.started = True

    def deleteLater(self):
        pass


def test_file_analysis_details_history_and_back_flow(tmp_path, monkeypatch, application):
    workspace = tmp_path / "presentations"
    workspace.mkdir()
    selected_path = workspace / "긴_한글_파일명_검사용_프레젠테이션.pptx"
    selected_path.touch()
    live_report = AnalysisReport(
        deck=DeckSnapshot(
            source_path=str(selected_path),
            source_sha256="live-sha",
            slide_count=3,
            width_pt=960.0,
            height_pt=540.0,
        ),
        findings=[
            Finding(
                id="typography:3:2",
                rule_id="typography",
                shape_key="3:2",
                severity="review",
                message="권장 크기보다 작습니다",
                measured=16.0,
                expected=18.0,
                evidence="16pt는 발표용 권장 크기 18pt보다 작습니다.",
            )
        ],
        executed_rules=("contrast", "typography", "layout"),
        status="analyzed",
    )
    history_report = AnalysisReport(
        deck=DeckSnapshot(
            source_path=str(tmp_path / "삭제된_과거_파일.pptx"),
            source_sha256="history-sha",
            slide_count=2,
            width_pt=960.0,
            height_pt=540.0,
        ),
        findings=[
            Finding(
                id="contrast:2:1",
                rule_id="contrast",
                shape_key="2:1",
                severity="unknown",
                message="명암비를 판정할 수 없습니다",
                measured=None,
                expected=None,
                evidence="배경색을 확인할 수 없습니다.",
            )
        ],
        executed_rules=("contrast", "typography", "layout"),
        status="analyzed",
    )
    history = FakeReportRepository(
        [
            StoredReport(9, "2026-11-06T08:30:00+09:00", history_report),
            StoredReport(8, "2026-11-05T08:30:00+09:00", live_report),
        ]
    )
    settings = FakeSettingsRepository(WorkspaceSettings(workspace, False))
    workers = []

    def worker_factory(path, parent=None):
        worker = ControlledWorker(path, parent)
        workers.append(worker)
        return worker

    monkeypatch.setattr(desktop, "build_workspace_settings_repository", lambda: settings)
    monkeypatch.setattr(desktop, "build_report_repository", lambda: history)
    monkeypatch.setattr(desktop, "AnalysisWorker", worker_factory)

    window = desktop.MainWindow()
    try:
        window.file_table.selectRow(0)
        application.processEvents()
        assert window.selected_file == selected_path
        assert window.analyze_button.isEnabled()
        assert window.file_table.columnCount() == 1
        assert window.file_table.selectedRanges()[0].columnCount() == 1

        window.start_analysis()
        assert workers[0].started is True
        assert window.inspection_stack.currentWidget() is window.progress_page
        assert window.choose_button.isEnabled() is False
        assert window.analyze_button.isEnabled() is False

        workers[0].succeeded.emit(live_report)
        assert window.result_table.rowCount() == 1
        window.result_table.selectRow(0)
        application.processEvents()
        assert "16.0" in window.detail_values.text()
        assert "18.0" in window.detail_values.text()
        assert "권장 크기" in window.detail_reason.text()
        assert window.result_table.selectedRanges()[0].columnCount() == 4
        assert window.result_table.cellWidget(0, 2) is not None

        workers[0].finished.emit()
        assert window.choose_button.isEnabled() is True

        window.show_history_list()
        assert window.left_stack.currentWidget() is window.history_left_page
        window.history_table.selectRow(0)
        application.processEvents()
        assert window.result_table.item(0, 2).text() == "판정 불가"
        assert "과거 이력 결과" in window.history_banner.text()
        assert "삭제된_과거_파일.pptx" in window.selected_file_label.text()
        assert window.history_table.columnCount() == 1

        window.history_table.selectRow(1)
        application.processEvents()
        assert selected_path.name in window.selected_file_label.text()

        window.show_file_tab()
        assert window.left_stack.currentWidget() is window.files_left_page
    finally:
        window.close()


def test_history_clear_cancel_then_confirm_preserves_workspace(tmp_path, monkeypatch, application):
    workspace = tmp_path / "presentations"
    workspace.mkdir()
    deck = DeckSnapshot(
        source_path=str(workspace / "history.pptx"),
        source_sha256="history-sha",
        slide_count=1,
        width_pt=960.0,
        height_pt=540.0,
    )
    report = AnalysisReport(deck=deck, executed_rules=("contrast",), status="analyzed")
    history = FakeReportRepository(
        [
            StoredReport(2, "2026-11-06T09:00:00+09:00", report),
            StoredReport(1, "2026-11-05T09:00:00+09:00", report),
        ]
    )
    settings = FakeSettingsRepository(WorkspaceSettings(workspace, True))
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    prompts = []

    def answer_question(*args):
        prompts.append(args[2])
        return answers.pop(0)

    monkeypatch.setattr(desktop, "build_workspace_settings_repository", lambda: settings)
    monkeypatch.setattr(desktop, "build_report_repository", lambda: history)
    monkeypatch.setattr(QMessageBox, "question", answer_question)

    window = desktop.MainWindow()
    try:
        window.show_history_list()
        window.history_table.selectRow(0)
        application.processEvents()
        assert window.viewing_history is True
        assert window.clear_history_button.isEnabled() is True

        window.clear_history()
        assert history.clear_calls == 0
        assert window.history_table.rowCount() == 2
        assert window.viewing_history is True

        window.clear_history()
        assert history.clear_calls == 1
        assert window.history_table.rowCount() == 0
        assert window.clear_history_button.isEnabled() is False
        assert window.inspection_stack.currentWidget() is window.history_empty_page
        assert settings.load() == WorkspaceSettings(workspace, True)
        assert all("2건" in prompt for prompt in prompts)
        assert all("원본 PPTX 파일은 유지" in prompt for prompt in prompts)
    finally:
        window.close()


def test_unknown_empty_not_configured_and_failure_states_are_distinct(tmp_path, monkeypatch, application):
    monkeypatch.setattr(desktop, "build_workspace_settings_repository", FakeSettingsRepository)
    monkeypatch.setattr(desktop, "build_report_repository", FakeReportRepository)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    deck = DeckSnapshot(
        source_path=str(tmp_path / "states.pptx"),
        source_sha256="states-sha",
        slide_count=1,
        width_pt=960.0,
        height_pt=540.0,
    )

    window = desktop.MainWindow()
    try:
        window.show_report(AnalysisReport(deck=deck, executed_rules=("contrast",), status="analyzed"))
        assert "발견 항목 없음" in window.result_label.text()

        unknown = Finding(
            id="contrast:1:1",
            rule_id="contrast",
            shape_key="1:1",
            severity="unknown",
            message="판정할 수 없습니다",
            measured=None,
            expected=None,
            evidence="측정 정보가 없습니다.",
        )
        window.show_report(
            AnalysisReport(
                deck=deck,
                findings=[unknown],
                executed_rules=("contrast",),
                status="analyzed",
            )
        )
        assert window.result_table.item(0, 2).text() == "판정 불가"
        assert "확인할 수 없음" in window.detail_values.text()

        window.show_report(AnalysisReport(deck=deck, status="not_configured"))
        assert "검사 규칙이 설정되지 않아" in window.result_label.text()

        window.show_analysis_error("reader failed")
        assert window.inspection_stack.currentWidget() is window.error_page
        assert "reader failed" in window.error_page.description_label.text()
        assert warnings
    finally:
        window.close()


def test_legacy_workspace_settings_gain_default_analysis_purpose(tmp_path):
    database_path = tmp_path / "legacy-settings.sqlite3"
    workspace = tmp_path / "legacy-workspace"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE workspace_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                workspace_path TEXT NOT NULL,
                recursive INTEGER NOT NULL CHECK (recursive IN (0, 1))
            )
            """
        )
        connection.execute(
            "INSERT INTO workspace_settings (id, workspace_path, recursive) VALUES (1, ?, 1)",
            (str(workspace),),
        )

    restored = SqliteWorkspaceSettingsRepository(database_path).load()

    assert restored == WorkspaceSettings(workspace, True, "presentation")


def test_saved_analysis_purpose_builds_the_next_analysis_policy(tmp_path, monkeypatch):
    settings = FakeSettingsRepository(WorkspaceSettings(tmp_path, False, "lecture"))
    monkeypatch.setattr(bootstrap, "build_workspace_settings_repository", lambda: settings)

    assert bootstrap.build_analysis_policy().purpose is Purpose.LECTURE
