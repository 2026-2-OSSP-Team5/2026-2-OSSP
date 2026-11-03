from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.bootstrap import report_database_path
from slideguard.domain.models import AnalysisReport, Box, DeckSnapshot, Finding


def make_report(status="analyzed"):
    return AnalysisReport(
        deck=DeckSnapshot(
            source_path=r"C:\presentations\demo.pptx",
            source_sha256="abc123",
            slide_count=2,
            width_pt=960.0,
            height_pt=540.0,
            limitations=("example limitation",),
        ),
        findings=[
            Finding(
                id="finding-1",
                rule_id="font-size",
                shape_key="1:2",
                severity="warning",
                message="Text is small",
                measured=12.0,
                expected=18.0,
                evidence="12pt < 18pt",
                box=Box(1.0, 2.0, 3.0, 4.0),
            )
        ] if status == "analyzed" else [],
        executed_rules=("font-size",) if status == "analyzed" else (),
        status=status,
    )


def test_save_and_read_report(tmp_path):
    repository = SqliteReportRepository(tmp_path / "history.sqlite3")
    expected = make_report()

    repository.save(expected)

    stored = repository.list_recent()
    assert len(stored) == 1
    assert stored[0].report == expected


def test_new_repository_instance_reads_previous_report(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    SqliteReportRepository(database_path).save(make_report())

    stored = SqliteReportRepository(database_path).list_recent()

    assert stored[0].report.deck.source_sha256 == "abc123"


def test_not_configured_and_analyzed_statuses_remain_distinct(tmp_path):
    repository = SqliteReportRepository(tmp_path / "history.sqlite3")
    repository.save(make_report("not_configured"))
    repository.save(make_report("analyzed"))

    statuses = [item.report.status for item in repository.list_recent()]

    assert statuses == ["analyzed", "not_configured"]


def test_report_database_path_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    app_data = tmp_path / "user-app-data"
    first_working_directory = tmp_path / "first"
    second_working_directory = tmp_path / "second"
    first_working_directory.mkdir()
    second_working_directory.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(app_data))

    monkeypatch.chdir(first_working_directory)
    first_path = report_database_path()
    monkeypatch.chdir(second_working_directory)
    second_path = report_database_path()

    assert first_path == second_path == app_data / "SlideGuard" / "reports.sqlite3"
