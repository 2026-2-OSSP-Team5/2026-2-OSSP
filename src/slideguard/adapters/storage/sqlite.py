from pathlib import Path

from slideguard.domain.models import AnalysisReport


class SqliteReportRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def save(self, report: AnalysisReport) -> None:
        raise NotImplementedError("Report history storage is not available")
