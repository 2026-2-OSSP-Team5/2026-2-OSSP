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
    Shape,
    TextRun,
)


@dataclass(frozen=True)
class StoredReport:
    id: int
    created_at: str
    report: AnalysisReport


class SqliteReportRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def save(self, report: AnalysisReport) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
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
            self._create_schema(connection)
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

    @staticmethod
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


def _box(data: dict | None) -> Box | None:
    return Box(**data) if data is not None else None


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
                        color=tuple(run["color"]) if run.get("color") is not None else None,
                        bold=run.get("bold"),
                    )
                    for run in shape_data.get("runs", [])
                ),
                background=(
                    tuple(shape_data["background"])
                    if shape_data.get("background") is not None
                    else None
                ),
                rendered_box=_box(shape_data.get("rendered_box")),
                supported=shape_data.get("supported", False),
                notes=tuple(shape_data.get("notes", [])),
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
