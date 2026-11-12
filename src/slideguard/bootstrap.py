from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.adapters.storage.sqlite import SqliteReportRepository, SqliteWorkspaceSettingsRepository
from slideguard.application.analyze import AnalyzePresentation
from slideguard.application.collect import SnapshotCollector
from slideguard.application.ports import (
    CollectionFailureCallback,
    DeckReader,
    FileWatchCallback,
    SavedRevision,
    SnapshotCallback,
    SuccessfulAnalysisHashes,
)
from slideguard.checks.contrast import ContrastRule
from slideguard.checks.layout import LayoutRule
from slideguard.checks.typography import TypographyRule
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning import MinimumChangePlanner

if TYPE_CHECKING:
    from slideguard.adapters.watching.monitor import SavedFileMonitor


def build_analyzer(reader: DeckReader | None = None) -> AnalyzePresentation:
    """Use the same rules for saved COM snapshots and the metadata-only CLI."""
    return AnalyzePresentation(
        reader=reader or OoxmlDeckReader(),
        rules=(ContrastRule(), TypographyRule(), LayoutRule()),
        planner=MinimumChangePlanner(),
    )


def build_presentation_patcher():
    from slideguard.adapters.powerpoint.patcher import PowerPointPatcher

    return PowerPointPatcher()


def report_database_path() -> Path:
    """Return a stable, per-user writable location for application data."""
    app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".local" / "share"
    return base / "SlideGuard" / "reports.sqlite3"


def build_report_repository() -> SqliteReportRepository:
    return SqliteReportRepository(report_database_path())


def build_workspace_settings_repository() -> SqliteWorkspaceSettingsRepository:
    return SqliteWorkspaceSettingsRepository(report_database_path())


def build_analysis_policy(path: Path | None = None) -> AnalysisPolicy:
    settings = build_workspace_settings_repository().load()
    return settings.policy_for(path) if settings is not None else AnalysisPolicy()


class PolicyAnalysisHashes:
    def last_successful_sha256(self, path: Path) -> str | None:
        return build_report_repository().last_successful_sha256(path, build_analysis_policy(path).cache_key)


def build_powerpoint_reader(*, render: bool = False):
    from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
    from slideguard.adapters.powerpoint.session import PowerPointReadSession

    return PowerPointDeckReader(PowerPointReadSession(), render=render)


class SnapshotCollection:
    """Lifecycle of the separately assembled watcher and snapshot worker."""

    def __init__(self, monitor: SavedFileMonitor, collector: SnapshotCollector):
        self.monitor = monitor
        self.collector = collector

    @property
    def is_running(self) -> bool:
        return self.monitor.is_running and self.collector.is_running

    def start(self) -> None:
        try:
            # Capture changes before the collector lists startup files. Its in-memory
            # queue also accepts events before its worker starts.
            self.monitor.start()
            self.collector.start()
        except BaseException as error:
            try:
                self.stop()
            except BaseException as cleanup_error:
                error.add_note(f"Collection startup cleanup failed: {cleanup_error}")
            raise

    def stop(self) -> None:
        # Invalidate in-flight output immediately, then stop the producer before
        # waiting for synchronous COM cleanup. Late monitor events are ignored.
        self.collector.stop(wait=False)
        try:
            self.monitor.stop()
        finally:
            self.collector.stop()


def build_snapshot_collection(
    folder: Path,
    on_snapshot: SnapshotCallback,
    *,
    recursive: bool = False,
    successful_hashes: SuccessfulAnalysisHashes | None = None,
    on_failure: CollectionFailureCallback | None = None,
    on_detected: Callable[[Path], None] | None = None,
    on_watch_event: FileWatchCallback | None = None,
    on_collecting: Callable[[Path], None] | None = None,
    settle_seconds: float = 0.8,
    max_attempts: int = 3,
    retry_delay_seconds: float = 0.2,
    render: bool = True,
) -> SnapshotCollection:
    """Collect complete saved-file snapshots for the analysis consumer."""
    from slideguard.adapters.watching.catalog import SavedFileCatalog
    from slideguard.adapters.watching.monitor import SavedFileMonitor

    collector = SnapshotCollector(
        reader=build_powerpoint_reader(render=render),
        source=SavedFileCatalog(folder, recursive=recursive),
        on_snapshot=on_snapshot,
        successful_hashes=successful_hashes,
        on_failure=on_failure,
        on_collecting=on_collecting,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )

    def handle_event(event):
        if on_detected is not None and isinstance(event, SavedRevision):
            on_detected(event.source_path)
        if on_watch_event is not None:
            on_watch_event(event)
        collector.on_event(event)

    monitor = SavedFileMonitor(
        folder,
        on_event=handle_event,
        recursive=recursive,
        settle_seconds=settle_seconds,
    )
    return SnapshotCollection(monitor, collector)
