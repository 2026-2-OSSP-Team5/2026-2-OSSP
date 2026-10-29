"""Short-lived, thread-local COM reads of caller-owned temporary PPTX copies."""

import math
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun

from .errors import (
    PowerPointBusyError,
    PowerPointCleanupError,
    PowerPointConnectionError,
    PowerPointExtractionError,
    PowerPointNotInstalledError,
    PowerPointOpenError,
    PowerPointSessionError,
    PowerPointUnavailableError,
)

_BUSY_HRESULTS = {0x80010001, 0x8001010A}  # CALL_REJECTED, SERVERCALL_RETRYLATER
_NOT_INSTALLED_HRESULTS = {0x80040154, 0x800401F3}  # CLASSNOTREG, CLASSSTRING
_STAGE_ERRORS = {
    "initialize": PowerPointConnectionError,
    "connect": PowerPointConnectionError,
    "open": PowerPointOpenError,
    "extract": PowerPointExtractionError,
    "close": PowerPointCleanupError,
    "uninitialize": PowerPointCleanupError,
}


class _WindowsComRuntime:
    def __init__(self):
        import pythoncom
        import win32com.client

        self.pythoncom = pythoncom
        self.client = win32com.client
        self.com_error = pythoncom.com_error

    def initialize(self):
        self.pythoncom.CoInitializeEx(self.pythoncom.COINIT_APARTMENTTHREADED)

    def connect(self):
        # DispatchEx is an activation request, not proof of exclusive process ownership.
        return self.client.DispatchEx("PowerPoint.Application")

    def pump_messages(self):
        self.pythoncom.PumpWaitingMessages()

    def uninitialize(self):
        self.pythoncom.CoUninitialize()


def _load_runtime():
    if sys.platform != "win32":
        raise PowerPointUnavailableError("load", "PowerPoint COM reading requires Windows")
    failure = None
    try:
        return _WindowsComRuntime()
    except (ImportError, OSError) as error:
        failure = PowerPointUnavailableError("load", f"pywin32 could not be loaded: {error}")
    raise failure from None


def _plain_snapshot(snapshot: DeckSnapshot) -> DeckSnapshot:
    def copy(value):
        if type(value) in (str, int, float, bool, type(None)):
            return value
        if type(value) is tuple:
            return tuple(copy(item) for item in value)
        if type(value) in (DeckSnapshot, Shape, TextRun, Box):
            return type(value)(**{field.name: copy(getattr(value, field.name)) for field in fields(value)})
        raise TypeError("Snapshot fields must contain only domain models, tuples and plain Python scalars")

    if type(snapshot) is not DeckSnapshot:
        raise TypeError("The extractor must return a DeckSnapshot")
    return copy(snapshot)


@dataclass(frozen=True)
class PowerPointReadSession:
    max_attempts: int = 3
    retry_delay_seconds: float = 0.1

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and nonnegative")

    def read_copy(self, temporary_copy: Path, extract: Callable[[Any], DeckSnapshot]) -> DeckSnapshot:
        """Run a read-only extractor synchronously on this calling worker thread.

        The caller supplies a private saved .pptx copy and handles its bytes/hash/lifetime.
        The extractor must not mutate or retain COM objects, including in closures. Only
        a detached DeckSnapshot leaves this method. No persistent STA or queue is kept.
        """
        path = Path(temporary_copy).resolve()
        if path.suffix.lower() != ".pptx":
            raise PowerPointOpenError("open", "Only saved .pptx copies are supported")
        runtime = _load_runtime()
        self._call(runtime, "initialize", runtime.initialize, retry=False, pump=False)
        application = presentation = None
        primary_error = None
        try:
            application = self._call(runtime, "connect", runtime.connect)
            presentation = self._call(
                runtime,
                "open",
                lambda: application.Presentations.Open(str(path), ReadOnly=-1, Untitled=-1, WithWindow=0),
            )
            if presentation is None:
                raise PowerPointOpenError("open", "PowerPoint did not return a presentation")
            # Retrying the whole extractor could repeat its side effects. A later read
            # may retry the session; property-level retries belong to the extractor.
            return self._call(runtime, "extract", lambda: _plain_snapshot(extract(presentation)), retry=False)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_errors = []
            try:
                if presentation is not None:
                    try:
                        self._call(runtime, "close", lambda: presentation.Close(), pump=False)
                    except PowerPointSessionError as error:
                        cleanup_errors.append(error)
            finally:
                # Release proxies on their owning thread before balancing CoInitializeEx.
                presentation = application = None
                try:
                    self._call(runtime, "uninitialize", runtime.uninitialize, retry=False, pump=False)
                except PowerPointSessionError as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                if primary_error is None:
                    primary_error = cleanup_errors.pop(0)
                    primary_error.cleanup_errors = tuple(cleanup_errors)
                    raise primary_error
                if isinstance(primary_error, PowerPointSessionError):
                    primary_error.cleanup_errors = tuple(cleanup_errors)
                for error in cleanup_errors:
                    primary_error.add_note(str(error))

    def _call(self, runtime, operation: str, call: Callable, *, retry=True, pump=True):
        limit = self.max_attempts if retry else 1
        for attempt in range(1, limit + 1):
            failure = None
            busy = False
            try:
                if pump:
                    runtime.pump_messages()
                return call()
            except Exception as error:
                hresult = None
                if isinstance(error, runtime.com_error):
                    hresult = error.hresult & 0xFFFFFFFF
                busy = hresult in _BUSY_HRESULTS
                error_type = _STAGE_ERRORS[operation]
                if operation == "connect" and hresult in _NOT_INSTALLED_HRESULTS:
                    error_type = PowerPointNotInstalledError
                elif busy:
                    error_type = PowerPointBusyError
                failure = error_type(operation, str(error), hresult=hresult, attempts=attempt)
                # An extractor traceback may own child COM proxies. Do not retain it
                # in a chained exception after the apartment has been uninitialized.
                traceback.clear_frames(error.__traceback__)
            except BaseException as error:
                traceback.clear_frames(error.__traceback__)
                raise
            if not busy or attempt == limit:
                # Outside the except block: the original COM exception is not chained.
                raise failure from None
            self._wait(runtime, operation)

    def _wait(self, runtime, operation: str):
        deadline = time.monotonic() + self.retry_delay_seconds
        while True:
            self._call(runtime, operation, runtime.pump_messages, retry=False, pump=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))
