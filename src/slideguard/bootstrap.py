from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.application.analyze import AnalyzePresentation
from slideguard.application.collect import SnapshotCollector
from slideguard.application.ports import (
    CollectionFailureCallback,
    SnapshotCallback,
    SuccessfulAnalysisHashes,
)

if TYPE_CHECKING:
    from slideguard.adapters.watching.monitor import SavedFileMonitor


def build_analyzer() -> AnalyzePresentation:
    """Build the analyzer with the configured reader and rules."""
    return AnalyzePresentation(reader=OoxmlDeckReader(), rules=())


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
    settle_seconds: float = 0.8,
    max_attempts: int = 3,
    retry_delay_seconds: float = 0.2,
) -> SnapshotCollection:
    """Opt-in saved-file COM collection; the inspect/OOXML analyzer is unchanged."""
    from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
    from slideguard.adapters.powerpoint.session import PowerPointReadSession
    from slideguard.adapters.watching.catalog import SavedFileCatalog
    from slideguard.adapters.watching.monitor import SavedFileMonitor

    collector = SnapshotCollector(
        reader=PowerPointDeckReader(PowerPointReadSession()),
        source=SavedFileCatalog(folder, recursive=recursive),
        on_snapshot=on_snapshot,
        successful_hashes=successful_hashes,
        on_failure=on_failure,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    monitor = SavedFileMonitor(
        folder,
        on_event=collector.on_event,
        recursive=recursive,
        settle_seconds=settle_seconds,
    )
    return SnapshotCollection(monitor, collector)
