import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from slideguard.adapters.storage.sqlite import (
    SqliteWorkspaceSettingsRepository,
    StoredReport,
    WorkspaceSettings,
)
from slideguard.domain.models import AnalysisReport, DeckSnapshot, Finding
from slideguard.ui import desktop


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class FakeReportRepository:
    def __init__(self, reports=None):
        self.reports = reports or []

    def list_recent(self):
        return self.reports


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
        assert window.result_table.item(0, 0).text() == "1:1"
        assert "deleted.pptx" in window.result_label.text()
    finally:
        window.close()
