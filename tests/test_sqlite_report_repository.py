import json
import sqlite3

from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.bootstrap import report_database_path
from slideguard.domain.models import (
    AnalysisReport,
    Box,
    DeckSnapshot,
    Finding,
    Measurement,
    ObjectEvidence,
    ObjectLocator,
    Shape,
    SlideDetails,
    TextRun,
    TextSpan,
)


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


def test_model_0_3_report_round_trips_through_new_repository_instance(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    evidence = ObjectEvidence(
        kind="group",
        origin="powerpoint",
        state="partial",
        properties=(("visible", True),),
        children=(
            ObjectEvidence(
                kind="shape",
                origin="ooxml",
                state="saved",
                text="본문",
                box=Box(10.0, 20.0, 30.0, 40.0),
                corners=((10.0, 20.0), (40.0, 60.0)),
            ),
        ),
    )
    expected = AnalysisReport(
        deck=DeckSnapshot(
            source_path=r"C:\presentations\expanded.pptx",
            source_sha256="expanded-sha",
            slide_count=1,
            width_pt=960.0,
            height_pt=540.0,
            shapes=(
                Shape(
                    slide_number=1,
                    shape_id=7,
                    name="Title",
                    box=Box(1.0, 2.0, 3.0, 4.0),
                    text="제목",
                    runs=(
                        TextRun(
                            text="제목",
                            font_size_pt=20.0,
                            color=(1, 2, 3),
                            bold=True,
                            source_color=(4, 5, 6),
                            transparency=0.25,
                            fill_kind="solid",
                            fill_visible=True,
                        ),
                    ),
                    background=(250, 250, 250),
                    rendered_box=Box(2.0, 3.0, 4.0, 5.0),
                    supported=True,
                    notes=("measured",),
                    locator=ObjectLocator((7, 8), "table_cell", (1, 2)),
                    visible=True,
                    effective_visible=True,
                    shape_type=19,
                    placeholder_type=1,
                    z_order=3,
                    content_box=Box(3.0, 4.0, 5.0, 6.0),
                    fill_color=(10, 20, 30),
                    fill_transparency=0.1,
                    properties=(("rotation", 0.0),),
                    measurements=(Measurement("text_box", "measured", "PowerPoint"),),
                    spans=(TextSpan("line", 1, "제목", Box(4.0, 5.0, 6.0, 7.0)),),
                    evidence=(evidence,),
                    visible_object_box=Box(5.0, 6.0, 7.0, 8.0),
                    visible_text_box=Box(6.0, 7.0, 8.0, 9.0),
                    background_samples=((0.5, 0.5, 240, 241, 242),),
                ),
            ),
            limitations=("rendering unavailable",),
            slides=(
                SlideDetails(
                    number=1,
                    hidden=False,
                    title="제목",
                    properties=(("layout", "Title Slide"),),
                    objects=(evidence,),
                ),
            ),
        ),
        findings=[],
        executed_rules=("contrast", "typography", "layout"),
        status="analyzed",
    )

    SqliteReportRepository(database_path).save(expected)
    restored = SqliteReportRepository(database_path).list_recent()[0].report

    assert restored == expected
    assert restored.schema_version == "0.3"


def test_legacy_model_0_1_history_remains_readable(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    repository = SqliteReportRepository(database_path)
    repository.save(make_report("not_configured"))
    legacy_payload = {
        "deck": {
            "source_path": r"C:\presentations\legacy.pptx",
            "source_sha256": "legacy-sha",
            "slide_count": 1,
            "width_pt": 960.0,
            "height_pt": 540.0,
            "shapes": [
                {
                    "slide_number": 1,
                    "shape_id": 2,
                    "name": "Legacy",
                    "box": {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
                    "runs": [{"text": "old", "color": [1, 2, 3]}],
                }
            ],
        },
        "findings": [],
        "proposals": [],
        "executed_rules": [],
        "status": "not_configured",
        "schema_version": "0.1",
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE analysis_reports SET report_json = ? WHERE id = 1",
            (json.dumps(legacy_payload),),
        )

    restored = SqliteReportRepository(database_path).list_recent()[0].report

    assert restored.schema_version == "0.1"
    assert restored.deck.source_sha256 == "legacy-sha"
    assert restored.deck.slides == ()
    assert restored.deck.shapes[0].locator is None
    assert restored.deck.shapes[0].runs[0].source_color is None


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
