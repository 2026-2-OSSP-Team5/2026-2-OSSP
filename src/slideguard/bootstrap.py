import os
from pathlib import Path

from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.application.analyze import AnalyzePresentation


def build_analyzer() -> AnalyzePresentation:
    """Build the analyzer with the configured reader and rules."""
    return AnalyzePresentation(reader=OoxmlDeckReader(), rules=())


def report_database_path() -> Path:
    """Return a stable, per-user writable location for report history."""
    app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".local" / "share"
    return base / "SlideGuard" / "reports.sqlite3"


def build_report_repository() -> SqliteReportRepository:
    return SqliteReportRepository(report_database_path())
