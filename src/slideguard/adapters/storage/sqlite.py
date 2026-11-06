import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from slideguard.domain.models import (
    AnalysisReport,
    Box,
    ChangeProposal,
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


@dataclass(frozen=True)
class StoredReport:
    id: int
    created_at: str
    report: AnalysisReport


@dataclass(frozen=True)
class WorkspaceSettings:
    workspace: Path
    recursive: bool
    purpose: str = "presentation"


class SqliteReportRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def save(self, report: AnalysisReport) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            _create_schema(connection)
            connection.execute(
                """
                INSERT INTO analysis_reports (
                    created_at, source_path, source_sha256, status, report_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    report.deck.source_path,
                    report.deck.source_sha256,
                    report.status,
                    json.dumps(asdict(report), ensure_ascii=False),
                ),
            )

    def list_recent(self, limit: int = 100) -> list[StoredReport]:
        if limit < 1:
            return []
        if not self.database_path.exists():
            return []

        with sqlite3.connect(self.database_path) as connection:
            _create_schema(connection)
            rows = connection.execute(
                """
                SELECT id, created_at, report_json
                FROM analysis_reports
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [StoredReport(row[0], row[1], _report_from_json(row[2])) for row in rows]

    def clear(self) -> None:
        """Delete saved analysis reports while preserving workspace settings."""
        if not self.database_path.exists():
            return
        with sqlite3.connect(self.database_path) as connection:
            _create_schema(connection)
            connection.execute("DELETE FROM analysis_reports")


class SqliteWorkspaceSettingsRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def save(self, workspace: Path, recursive: bool, purpose: str | None = None) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            _create_schema(connection)
            if purpose is None:
                current = connection.execute(
                    "SELECT purpose FROM workspace_settings WHERE id = 1"
                ).fetchone()
                purpose = current[0] if current is not None else "presentation"
            connection.execute(
                """
                INSERT INTO workspace_settings (id, workspace_path, recursive, purpose)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workspace_path = excluded.workspace_path,
                    recursive = excluded.recursive,
                    purpose = excluded.purpose
                """,
                (str(workspace), int(recursive), purpose),
            )

    def load(self) -> WorkspaceSettings | None:
        if not self.database_path.exists():
            return None

        with sqlite3.connect(self.database_path) as connection:
            _create_schema(connection)
            row = connection.execute(
                "SELECT workspace_path, recursive, purpose FROM workspace_settings WHERE id = 1"
            ).fetchone()

        if row is None:
            return None
        return WorkspaceSettings(workspace=Path(row[0]), recursive=bool(row[1]), purpose=row[2])


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('not_configured', 'analyzed')),
            report_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            workspace_path TEXT NOT NULL,
            recursive INTEGER NOT NULL CHECK (recursive IN (0, 1)),
            purpose TEXT NOT NULL DEFAULT 'presentation'
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(workspace_settings)")}
    if "purpose" not in columns:
        connection.execute(
            "ALTER TABLE workspace_settings ADD COLUMN purpose TEXT NOT NULL DEFAULT 'presentation'"
        )


def _box(data: dict | None) -> Box | None:
    return Box(**data) if data is not None else None


def _color(data: list[int] | tuple[int, ...] | None) -> tuple[int, int, int] | None:
    return tuple(data) if data is not None else None


def _properties(data: list[list] | None) -> tuple[tuple, ...]:
    return tuple(tuple(item) for item in (data or []))


def _locator(data: dict | None) -> ObjectLocator | None:
    if data is None:
        return None
    return ObjectLocator(
        shape_path=tuple(data["shape_path"]),
        kind=data.get("kind", "shape"),
        indices=tuple(data.get("indices", [])),
    )


def _measurement(data: dict) -> Measurement:
    return Measurement(
        field=data["field"],
        state=data["state"],
        evidence=data["evidence"],
    )


def _object_evidence(data: dict) -> ObjectEvidence:
    return ObjectEvidence(
        kind=data["kind"],
        origin=data["origin"],
        state=data["state"],
        text=data.get("text", ""),
        box=_box(data.get("box")),
        properties=_properties(data.get("properties")),
        children=tuple(_object_evidence(item) for item in data.get("children", [])),
        corners=tuple(tuple(corner) for corner in data.get("corners", [])),
    )


def _text_span(data: dict) -> TextSpan:
    return TextSpan(
        kind=data["kind"],
        index=data["index"],
        text=data["text"],
        box=_box(data.get("box")),
    )


def _slide_details(data: dict) -> SlideDetails:
    return SlideDetails(
        number=data["number"],
        hidden=data.get("hidden"),
        title=data.get("title"),
        properties=_properties(data.get("properties")),
        objects=tuple(_object_evidence(item) for item in data.get("objects", [])),
    )


def _report_from_json(payload: str) -> AnalysisReport:
    data = json.loads(payload)
    deck_data = data["deck"]
    shapes = []
    for shape_data in deck_data.get("shapes", []):
        shapes.append(
            Shape(
                slide_number=shape_data["slide_number"],
                shape_id=shape_data["shape_id"],
                name=shape_data["name"],
                box=_box(shape_data["box"]),
                text=shape_data.get("text", ""),
                runs=tuple(
                    TextRun(
                        text=run["text"],
                        font_size_pt=run.get("font_size_pt"),
                        color=_color(run.get("color")),
                        bold=run.get("bold"),
                        source_color=_color(run.get("source_color")),
                        transparency=run.get("transparency"),
                        fill_kind=run.get("fill_kind"),
                        fill_visible=run.get("fill_visible"),
                    )
                    for run in shape_data.get("runs", [])
                ),
                background=_color(shape_data.get("background")),
                rendered_box=_box(shape_data.get("rendered_box")),
                supported=shape_data.get("supported", False),
                notes=tuple(shape_data.get("notes", [])),
                locator=_locator(shape_data.get("locator")),
                visible=shape_data.get("visible"),
                effective_visible=shape_data.get("effective_visible"),
                shape_type=shape_data.get("shape_type"),
                placeholder_type=shape_data.get("placeholder_type"),
                z_order=shape_data.get("z_order"),
                content_box=_box(shape_data.get("content_box")),
                fill_color=_color(shape_data.get("fill_color")),
                fill_transparency=shape_data.get("fill_transparency"),
                properties=_properties(shape_data.get("properties")),
                measurements=tuple(
                    _measurement(item) for item in shape_data.get("measurements", [])
                ),
                spans=tuple(_text_span(item) for item in shape_data.get("spans", [])),
                evidence=tuple(
                    _object_evidence(item) for item in shape_data.get("evidence", [])
                ),
                visible_object_box=_box(shape_data.get("visible_object_box")),
                visible_text_box=_box(shape_data.get("visible_text_box")),
                background_samples=tuple(
                    tuple(sample) for sample in shape_data.get("background_samples", [])
                ),
            )
        )

    deck = DeckSnapshot(
        source_path=deck_data["source_path"],
        source_sha256=deck_data["source_sha256"],
        slide_count=deck_data["slide_count"],
        width_pt=deck_data["width_pt"],
        height_pt=deck_data["height_pt"],
        shapes=tuple(shapes),
        limitations=tuple(deck_data.get("limitations", [])),
        slides=tuple(_slide_details(item) for item in deck_data.get("slides", [])),
    )
    findings = [
        Finding(
            id=item["id"],
            rule_id=item["rule_id"],
            shape_key=item["shape_key"],
            severity=item["severity"],
            message=item["message"],
            measured=item.get("measured"),
            expected=item.get("expected"),
            evidence=item["evidence"],
            box=_box(item.get("box")),
        )
        for item in data.get("findings", [])
    ]
    proposals = [
        ChangeProposal(
            id=item["id"],
            finding_id=item["finding_id"],
            shape_key=item["shape_key"],
            property=item["property"],
            before=tuple(item["before"]) if isinstance(item["before"], list) else item["before"],
            after=tuple(item["after"]) if isinstance(item["after"], list) else item["after"],
            cost=item["cost"],
        )
        for item in data.get("proposals", [])
    ]
    return AnalysisReport(
        deck=deck,
        findings=findings,
        proposals=proposals,
        executed_rules=tuple(data.get("executed_rules", [])),
        status=data.get("status", "not_configured"),
        schema_version=data.get("schema_version", "0.1"),
    )
