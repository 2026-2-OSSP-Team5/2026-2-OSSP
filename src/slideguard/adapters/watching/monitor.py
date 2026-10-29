import logging
import math
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

LOGGER = logging.getLogger("slideguard.watch")


@dataclass
class _PendingSave:
    last_change: float
    first_seen: float
    signature: tuple[int, int, int] | None = None
    last_attempt: float = 0
    waiting_logged: bool = False


def _signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _saved_revision(path: Path) -> tuple[str, tuple[int, int, int]]:
    """A readable, complete PPTX revision; a quiet interval is checked by the caller."""
    before = _signature(path)
    with path.open("rb") as stream:
        with zipfile.ZipFile(stream) as package:
            if "ppt/presentation.xml" not in package.namelist() or package.testzip() is not None:
                raise ValueError("Incomplete PPTX archive")
        stream.seek(0)
        digest = file_digest(stream, "sha256").hexdigest()
    if before != _signature(path):
        raise OSError("File changed while checking the saved revision")
    return digest, before


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
    """

    def __init__(
        self,
        path: Path,
        on_saved: Callable[[Path], None] | None = None,
        *,
        recursive: bool = False,
        settle_seconds: float = 0.8,
        retry_seconds: float = 10.0,
    ):
        for name, value in (("settle_seconds", settle_seconds), ("retry_seconds", retry_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        self.path = path.resolve()
        self.on_saved = on_saved
        self.recursive = recursive
        self.settle_seconds = settle_seconds
        self.retry_seconds = retry_seconds
        self._known: dict[Path, tuple[str, tuple[int, int, int]]] = {}
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
        try:
            relative = path.relative_to(self.path)
        except ValueError:
            return False
        return (
            path.suffix.lower() == ".pptx"
            and not path.name.startswith(("~$", ".", "~"))
            and (self.recursive or len(relative.parts) == 1)
        )

    def _files(self):
        candidates = self.path.rglob("*") if self.recursive else self.path.iterdir()
        return (p for p in candidates if self._accepts(p) and p.is_file())

    def _enqueue(self, path: Path) -> None:
        # Use absolute lexical paths so deleted/moved source paths can still be queued.
        path = path.absolute()
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

    def _reconcile(self) -> None:
        files = set(self._files())
        for path in files | self._known.keys():
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

    def start(self) -> None:
        if self._observer is not None:
            raise RuntimeError("Create a new monitor to start another watch session")
        if not self.path.is_dir():
            raise NotADirectoryError(f"Watch directory does not exist: {self.path}")
        # Existing files establish a baseline; they are not reported as new saves.
        for path in self._files():
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
        while not self._stopped.is_set():
            with self._condition:
                self._condition.wait(timeout=0.1)
                rescan, self._rescan = self._rescan, False
                pending = list(self._pending.items())
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
            # Delay deletion to tolerate atomic replace saves.
            if now - candidate.last_change >= self.settle_seconds:
                with self._condition:
                    if self._pending.get(path) is not candidate:
                        return
                    self._pending.pop(path)
                    existed = self._known.pop(path, None)
                if existed is not None:
                    LOGGER.info("FILE_REMOVED | file=%s", path)
            return
        except OSError as error:
            self._retry(path, candidate, error)
            return
        if candidate.signature is not None and signature != candidate.signature:
            candidate.last_change = now
        candidate.signature = signature
        if now - candidate.last_change < self.settle_seconds or now - candidate.last_attempt < 0.25:
            return
        candidate.last_attempt = now
        try:
            digest, signature = _saved_revision(path)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            self._retry(path, candidate, error)
            return
        with self._condition:
            if self._pending.get(path) is not candidate:
                return  # A later event arrived while reading this revision.
            previous = self._known.get(path)
            self._known[path] = (digest, signature)
            self._pending.pop(path)
        if previous is not None and previous[0] == digest:
            return
        LOGGER.info("SAVE_DETECTED | 저장 감지 | file=%s | sha256=%s | bytes=%d", path, digest, signature[0])
        if self.on_saved is not None:
            try:
                self.on_saved(path)
            except Exception:
                # A consumer failure must not terminate filesystem monitoring.
                LOGGER.exception("SAVE_CALLBACK_FAILED | file=%s", path)

    def _retry(self, path: Path, candidate: _PendingSave, error: Exception) -> None:
        if not candidate.waiting_logged:
            LOGGER.warning("SAVE_WAITING | file=%s | reason=%s", path, error)
            candidate.waiting_logged = True
        if time.monotonic() - candidate.first_seen >= self.retry_seconds:
            with self._condition:
                if self._pending.get(path) is candidate:
                    self._pending.pop(path)
            LOGGER.warning("SAVE_UNREADABLE | file=%s | retry on next change", path)

    def stop(self) -> None:
        self._stopped.set()
        with self._condition:
            self._condition.notify_all()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()
        LOGGER.info("WATCH_STOPPED | directory=%s", self.path)
