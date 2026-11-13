import logging
import math
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from slideguard.adapters.watching.paths import absolute_path, accepts_path, presentation_paths
from slideguard.adapters.watching.revisions import FileSignature
from slideguard.adapters.watching.revisions import file_signature as _signature
from slideguard.adapters.watching.revisions import saved_revision as _saved_revision
from slideguard.application.ports import FileRemoved, FileWatchCallback, FileWatchEvent, SavedRevision

LOGGER = logging.getLogger("slideguard.watch")


@dataclass
class _PendingSave:
    last_change: float
    first_seen: float
    signature: FileSignature | None = None
    last_attempt: float = 0
    waiting_logged: bool = False
    missing_since: float | None = None
    retry_count: int = 0
    next_attempt: float = 0


class _FolderEvents(FileSystemEventHandler):
    def __init__(self, monitor: "SavedFileMonitor"):
        self.monitor = monitor

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in {"created", "modified", "moved", "deleted"}:
            return
        if event.is_directory:
            # Windows may deliver a directory move without every child-file event.
            self.monitor._request_rescan()
            return
        for path in (event.src_path, getattr(event, "dest_path", "")):
            if path:
                self.monitor._enqueue(Path(path))


class SavedFileMonitor:
    """Detect stable changed PPTX revisions. This does not perform readability analysis.

    The in-memory baseline tracks last detected file bytes, not the last analysis result.
    Watchdog callbacks only queue paths; ZIP checks and user callbacks run in one worker.
    Event callbacks should enqueue and return promptly. Downstream retry belongs to
    the consumer; failed delivery does not roll back the last detected version.
    """

    def __init__(
        self,
        path: Path,
        on_saved: Callable[[Path], None] | None = None,
        *,
        on_event: FileWatchCallback | None = None,
        recursive: bool = False,
        settle_seconds: float = 0.8,
        retry_seconds: float = 10.0,
    ):
        for name, value in (("settle_seconds", settle_seconds), ("retry_seconds", retry_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        self.path = path.resolve()
        self.on_saved = on_saved
        self.on_event = on_event
        self.recursive = recursive
        self.settle_seconds = settle_seconds
        self.retry_seconds = retry_seconds
        self._known: dict[Path, tuple[SavedRevision, FileSignature]] = {}
        self._present: set[Path] = set()
        self._pending: dict[Path, _PendingSave] = {}
        self._condition = threading.Condition()
        self._stopped = threading.Event()
        self._rescan = False
        self._observer = None
        self._worker = None

    @property
    def is_running(self) -> bool:
        return (
            self._observer is not None
            and self._observer.is_alive()
            and self._worker is not None
            and self._worker.is_alive()
            and not self._stopped.is_set()
        )

    def _accepts(self, path: Path) -> bool:
        return accepts_path(self.path, path, recursive=self.recursive)

    def _files(self):
        return presentation_paths(self.path, recursive=self.recursive)

    def _enqueue(self, path: Path) -> None:
        # Use absolute lexical paths so deleted/moved source paths can still be queued.
        path = absolute_path(path)
        if not self._accepts(path):
            return
        now = time.monotonic()
        with self._condition:
            self._pending[path] = _PendingSave(now, now)
            self._condition.notify()

    def _request_rescan(self) -> None:
        with self._condition:
            self._rescan = True
            self._condition.notify()

    def request_retry(self, path: Path) -> None:
        """Retry a repaired/unreadable file even if no new filesystem event arrives."""
        if not self._stopped.is_set():
            self._enqueue(path)

    def _reconcile(self) -> None:
        files = set(self._files())
        for path in files | self._known.keys() | self._present:
            try:
                changed = path not in self._known or self._known[path][1] != _signature(path)
            except FileNotFoundError:
                changed = True
            if changed:
                # Preserve an already queued file's quiet interval during folder rescans.
                with self._condition:
                    pending = path in self._pending
                if not pending:
                    self._enqueue(path)

    def _reconcile_missing(self) -> None:
        # Windows can report only the old half of an out-of-scope folder rename,
        # which Watchdog cannot turn into a paired move/child deletion event.
        # Check known presence only; do not rescan folders or rehash file contents.
        for path in tuple(self._present):
            try:
                path.stat()
            except FileNotFoundError:
                with self._condition:
                    pending = path in self._pending
                if not pending:
                    self._enqueue(path)
            except OSError:
                continue  # Inaccessibility alone does not prove removal.

    def start(self) -> None:
        if self._observer is not None:
            raise RuntimeError("Create a new monitor to start another watch session")
        if not self.path.is_dir():
            raise NotADirectoryError(f"Watch directory does not exist: {self.path}")
        # Existing files establish a baseline; they are not reported as new saves.
        for path in self._files():
            self._present.add(path)
            try:
                self._known[path] = _saved_revision(path)
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
                self._enqueue(path)
        observer = Observer()
        observer.schedule(_FolderEvents(self), str(self.path), recursive=self.recursive)
        try:
            observer.start()
            self._observer = observer
            # Catch writes in the small gap between the baseline read and observer startup.
            self._reconcile()
            self._worker = threading.Thread(target=self._run, daemon=True, name="pptx-save-monitor")
            self._worker.start()
        except BaseException:
            observer.stop()
            if observer.is_alive():
                observer.join()
            self._observer = None
            raise
        LOGGER.info(
            "WATCH_STARTED | directory=%s | recursive=%s | settle=%.2fs | baseline=%d",
            self.path,
            self.recursive,
            self.settle_seconds,
            len(self._known),
        )

    def _run(self) -> None:
        next_presence_check = time.monotonic() + 1.0
        while not self._stopped.is_set():
            with self._condition:
                self._condition.wait(timeout=0.1)
                rescan, self._rescan = self._rescan, False
                pending = list(self._pending.items())
            if time.monotonic() >= next_presence_check:
                self._reconcile_missing()
                next_presence_check = time.monotonic() + 1.0
            if rescan:
                try:
                    self._reconcile()
                except OSError:
                    LOGGER.exception("WATCH_ERROR | cannot scan directory=%s", self.path)
            for path, candidate in pending:
                if self._stopped.is_set():
                    return
                self._check_pending(path, candidate)

    def _check_pending(self, path: Path, candidate: _PendingSave) -> None:
        now = time.monotonic()
        try:
            signature = _signature(path)
        except FileNotFoundError:
            # Start from observed absence, even when a deletion event arrives late.
            if candidate.missing_since is None:
                candidate.missing_since = now
                return
            if now - max(candidate.last_change, candidate.missing_since) >= self.settle_seconds:
                with self._condition:
                    if self._pending.get(path) is not candidate:
                        return
                    self._pending.pop(path)
                    existed = path in self._present
                    self._present.discard(path)
                    self._known.pop(path, None)
                if existed:
                    LOGGER.info("FILE_REMOVED | file=%s", path)
                    self._notify_event(FileRemoved(path))
            return
        except OSError as error:
            candidate.missing_since = None  # An unreadable path is not confirmed absence.
            if now >= candidate.next_attempt:
                self._retry(path, candidate, error)
            return
        with self._condition:
            self._present.add(path)
        if candidate.missing_since is not None:
            candidate.missing_since = None
            candidate.last_change = now
        if candidate.signature is not None and signature != candidate.signature:
            candidate.last_change = now
            candidate.first_seen = now
            candidate.retry_count = 0
            candidate.next_attempt = 0
        candidate.signature = signature
        if (
            now - candidate.last_change < self.settle_seconds
            or now - candidate.last_attempt < 0.25
            or now < candidate.next_attempt
        ):
            return
        candidate.last_attempt = now
        try:
            revision, signature = _saved_revision(path)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            self._retry(path, candidate, error)
            return
        with self._condition:
            if self._pending.get(path) is not candidate:
                return  # A later event arrived while reading this revision.
            previous = self._known.get(path)
            self._known[path] = (revision, signature)
            self._pending.pop(path)
        if previous is not None and previous[0].source_sha256 == revision.source_sha256:
            return
        LOGGER.info(
            "SAVE_DETECTED | 저장 감지 | file=%s | sha256=%s | bytes=%d",
            path,
            revision.source_sha256,
            revision.size_bytes,
        )
        self._notify_event(revision)
        if self.on_saved is not None:
            try:
                self.on_saved(path)
            except Exception:
                # A consumer failure must not terminate filesystem monitoring.
                LOGGER.exception("SAVE_CALLBACK_FAILED | file=%s", path)

    def _notify_event(self, event: FileWatchEvent) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                LOGGER.exception(
                    "EVENT_CALLBACK_FAILED | event=%s | file=%s", type(event).__name__, event.source_path
                )

    def _retry(self, path: Path, candidate: _PendingSave, error: Exception) -> None:
        with self._condition:
            if self._pending.get(path) is not candidate:
                return
        candidate.retry_count += 1
        candidate.next_attempt = time.monotonic() + min(5.0, 0.5 * 2 ** min(candidate.retry_count - 1, 4))
        if not candidate.waiting_logged:
            LOGGER.warning("SAVE_WAITING | file=%s | reason=%s", path, error)
            candidate.waiting_logged = True
        if time.monotonic() - candidate.first_seen >= self.retry_seconds:
            with self._condition:
                if self._pending.get(path) is candidate:
                    self._pending.pop(path)
            LOGGER.warning("SAVE_UNREADABLE | file=%s | retry on next change", path)

    def stop(self, *, wait: bool = True) -> None:
        self._stopped.set()
        with self._condition:
            self._condition.notify_all()
        if self._observer is not None:
            self._observer.stop()
            if wait:
                self._observer.join()
        if wait and self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()
        LOGGER.info("WATCH_STOPPED | directory=%s", self.path)
