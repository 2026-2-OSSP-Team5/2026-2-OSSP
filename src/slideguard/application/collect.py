import logging
import math
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slideguard.application.ports import (
    CollectionFailure,
    CollectionFailureCallback,
    CollectionStage,
    DeckReader,
    FileRemoved,
    FileWatchEvent,
    SavedFileSource,
    SavedRevision,
    SnapshotCallback,
    SuccessfulAnalysisHashes,
)

LOGGER = logging.getLogger("slideguard.collect")


@dataclass(eq=False)
class _Job:
    path: Path
    expected: SavedRevision | None = None
    initial: bool = False
    attempt: int = 0
    ready_at: float = 0


@dataclass
class _Scan:
    attempt: int = 0
    ready_at: float = 0


class SnapshotCollector:
    """Collect current saved snapshots on one worker, independently of watching.

    Events only update memory. All ports and delivery callbacks run on the worker.
    A job's identity is its generation: removal, replacement and explicit requests
    invalidate its result. Successful delivery does not mean successful analysis.
    """

    def __init__(
        self,
        reader: DeckReader,
        source: SavedFileSource,
        on_snapshot: SnapshotCallback,
        *,
        successful_hashes: SuccessfulAnalysisHashes | None = None,
        on_failure: CollectionFailureCallback | None = None,
        on_collecting: Callable[[Path], None] | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.2,
    ):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and nonnegative")
        self._reader = reader
        self._source = source
        self._on_snapshot = on_snapshot
        self._successful_hashes = successful_hashes
        self._on_failure = on_failure
        self._on_collecting = on_collecting
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._condition = threading.Condition()
        self._latest: dict[Path, _Job] = {}
        self._pending: dict[Path, _Job] = {}
        self._scan: _Scan | None = _Scan()
        self._touched: set[Path] = set()
        self._processing = False
        self._closed = False
        self._worker: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._worker is not None and self._worker.is_alive() and not self._closed

    def on_event(self, event: FileWatchEvent) -> None:
        with self._condition:
            if self._closed:
                return
            path = event.source_path
            if self._scan is not None:
                self._touched.add(path)
            if isinstance(event, FileRemoved):
                self._latest.pop(path, None)
                self._pending.pop(path, None)
            else:
                previous = self._latest.get(path)
                if previous is not None and not previous.initial and previous.expected == event:
                    return
                self._enqueue(_Job(path, expected=event))
            self._condition.notify_all()

    def request(self, path: Path) -> None:
        """Explicitly collect again, even after success/failure of identical bytes.

        Use the absolute path supplied by the watcher/catalog; resolving paths is
        the adapter's responsibility. Requests bypass the startup history filter.
        """
        with self._condition:
            if self._closed:
                return
            if self._scan is not None:
                self._touched.add(path)
            self._enqueue(_Job(path))

    def _enqueue(self, job: _Job) -> None:
        # Called with the condition held. Replacing the identity invalidates active work.
        self._latest[job.path] = job
        self._pending[job.path] = job
        self._condition.notify_all()

    def start(self) -> None:
        with self._condition:
            if self._worker is not None or self._closed:
                raise RuntimeError("Create a new collector to start another collection session")
            self._worker = threading.Thread(target=self._run, name="pptx-snapshot-collector", daemon=True)
            try:
                self._worker.start()
            except BaseException:
                self._worker = None
                raise

    def stop(self, *, wait: bool = True) -> None:
        """Invalidate queued/in-flight results; optionally wait for reader cleanup.

        An in-progress synchronous COM call cannot be forcibly interrupted.
        """
        with self._condition:
            self._closed = True
            self._latest.clear()
            self._pending.clear()
            self._scan = None
            self._touched.clear()
            worker = self._worker
            self._condition.notify_all()
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait for initial listing, queued retries and active delivery to finish."""
        if self._worker is threading.current_thread():
            raise RuntimeError("The collection worker cannot wait for itself")
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._processing and not self._latest and self._scan is None,
                timeout=timeout,
            )

    def _current(self, job: _Job) -> bool:
        with self._condition:
            return not self._closed and self._latest.get(job.path) is job

    def _finish(self, job: _Job) -> None:
        with self._condition:
            if self._latest.get(job.path) is job:
                self._latest.pop(job.path)
            self._condition.notify_all()

    def _next(self) -> _Job | _Scan | None:
        with self._condition:
            while not self._closed:
                now = time.monotonic()
                if self._scan is not None and self._scan.ready_at <= now:
                    self._processing = True
                    return self._scan
                for path, job in self._pending.items():
                    if job.ready_at <= now:
                        self._pending.pop(path)
                        self._processing = True
                        return job
                deadlines = [job.ready_at for job in self._pending.values()]
                if self._scan is not None:
                    deadlines.append(self._scan.ready_at)
                self._condition.wait(timeout=max(0, min(deadlines) - now) if deadlines else None)
            return None

    def _run(self) -> None:
        while (work := self._next()) is not None:
            try:
                if isinstance(work, _Scan):
                    self._scan_initial(work)
                else:
                    self._collect(work)
            finally:
                with self._condition:
                    self._processing = False
                    self._condition.notify_all()

    def _scan_initial(self, scan: _Scan) -> None:
        scan.attempt += 1
        failure = None
        try:
            paths = tuple(self._source.list_paths())
        except Exception as error:
            failure = self._failure_data(None, "list", error, scan.attempt)
        if failure is not None:
            with self._condition:
                if self._closed:
                    return
                if failure.retrying:
                    scan.ready_at = time.monotonic() + self._retry_delay_seconds
                else:
                    self._scan = None
                    self._touched.clear()
            self._notify_failure(failure)
            return
        with self._condition:
            if self._closed:
                return
            for path in paths:
                if path not in self._touched and path not in self._latest:
                    self._enqueue(_Job(path, initial=True))
            self._scan = None
            self._touched.clear()

    def _revision(self, job: _Job) -> SavedRevision:
        revision = self._source.current_revision(job.path)
        if revision is None:
            raise FileNotFoundError("Saved presentation is currently absent")
        if revision.source_path != job.path:
            raise ValueError("Revision source path does not match the requested path")
        return revision

    def _matches(self, job: _Job, revision: SavedRevision) -> bool:
        with self._condition:
            if not self._current(job):
                return False
            if job.expected is not None and job.expected != revision:
                self._enqueue(_Job(job.path, expected=revision, initial=job.initial))
                return False
            job.expected = revision
            return True

    def _collect(self, job: _Job) -> None:
        job.attempt += 1
        stage: CollectionStage = "version"
        failure = None
        try:
            if not self._current(job) or not self._matches(job, self._revision(job)):
                return
            if job.initial and self._successful_hashes is not None:
                stage = "history"
                previous = self._successful_hashes.last_successful_sha256(job.path)
                if not self._current(job):
                    return
                if previous == job.expected.source_sha256:
                    stage = "version"
                    if self._matches(job, self._revision(job)):
                        self._finish(job)
                    return
            stage = "read"
            if not self._current(job):
                return
            if self._on_collecting is not None:
                self._on_collecting(job.path)
            deck = self._reader.read(job.path)
            if not self._current(job):
                return
            stage = "version"
            if not self._matches(job, self._revision(job)):
                return
            stage = "verify"
            if deck.source_sha256 != job.expected.source_sha256 or Path(deck.source_path) != job.path:
                raise ValueError("Snapshot does not match the requested saved revision")
            stage = "deliver"
            # Handoff boundary. A later event cannot cancel a callback already entered;
            # downstream asynchronous work must retain and recheck the snapshot hash.
            if not self._current(job):
                return
            self._on_snapshot(deck)
        except Exception as error:
            failure = self._failure_data(job, stage, error, job.attempt)
        if failure is None:
            self._finish(job)
        else:
            with self._condition:
                if not self._current(job):
                    return
                if failure.retrying:
                    job.ready_at = time.monotonic() + self._retry_delay_seconds
                    self._pending[job.path] = job
                else:
                    self._latest.pop(job.path)
                self._condition.notify_all()
            self._notify_failure(failure)

    def _failure_data(
        self, job: _Job | None, stage: CollectionStage, error: Exception, attempt: int
    ) -> CollectionFailure:
        failure = CollectionFailure(
            source_path=job.path if job is not None else None,
            expected_sha256=job.expected.source_sha256 if job is not None and job.expected else None,
            stage=stage,
            error_type=type(error).__name__,
            message=str(error),
            attempt=attempt,
            retrying=attempt < self._max_attempts,
        )
        traceback.clear_frames(error.__traceback__)
        return failure

    def _notify_failure(self, failure: CollectionFailure) -> None:
        LOGGER.warning(
            "COLLECTION_FAILED | file=%s | stage=%s | attempt=%d | retrying=%s | reason=%s",
            failure.source_path,
            failure.stage,
            failure.attempt,
            failure.retrying,
            failure.message,
        )
        if self._on_failure is not None:
            try:
                self._on_failure(failure)
            except Exception:
                LOGGER.warning("COLLECTION_FAILURE_CALLBACK_FAILED | file=%s", failure.source_path)
