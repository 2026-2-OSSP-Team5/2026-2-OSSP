import builtins
import subprocess
import sys
import threading
import weakref
from collections import defaultdict, deque
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from slideguard.adapters.powerpoint import session
from slideguard.adapters.powerpoint.errors import (
    PowerPointBusyError,
    PowerPointCleanupError,
    PowerPointConnectionError,
    PowerPointExtractionError,
    PowerPointNotInstalledError,
    PowerPointOpenError,
    PowerPointUnavailableError,
)
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun


class FakeComError(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__("fake COM failure")


class FakePresentation:
    def __init__(self, runtime, *, user_owned=False):
        self.runtime = runtime
        self.user_owned = user_owned
        self.closed = False

    def Close(self):
        assert not self.user_owned, "The user's presentation must remain open"
        self.runtime.record("close")
        self.closed = True

    def Save(self):
        pytest.fail("Read sessions must never save a presentation")


class FakePresentations:
    def __init__(self, runtime):
        self.runtime = runtime

    def Open(self, path, **options):
        self.runtime.record("open")
        self.runtime.open_arguments.append((path, options))
        return self.runtime.presentation

    def Close(self):
        pytest.fail("Read sessions must never close the presentation collection")


class FakeApplication:
    def __init__(self, runtime):
        self.Presentations = FakePresentations(runtime)
        self.Visible = -1
        self.DisplayAlerts = 2

    def Quit(self):
        pytest.fail("Even DispatchEx may reuse the user's PowerPoint server")


class FakeRuntime:
    com_error = FakeComError

    def __init__(self):
        self.events = []
        self.thread_ids = []
        self.failures = defaultdict(deque)
        self.open_arguments = []
        self.presentation = FakePresentation(self)
        self.user_presentation = FakePresentation(self, user_owned=True)
        self.application = FakeApplication(self)

    def record(self, operation):
        self.events.append(operation)
        self.thread_ids.append(threading.get_ident())
        if self.failures[operation]:
            failure = self.failures[operation].popleft()
            if failure is not None:
                raise failure

    def initialize(self):
        self.record("initialize")

    def connect(self):
        self.record("connect")
        return self.application

    def pump_messages(self):
        self.record("pump")

    def uninitialize(self):
        self.record("uninitialize")


@pytest.fixture
def runtime(monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(session, "_load_runtime", lambda: runtime)
    return runtime


@pytest.fixture
def temporary_copy(tmp_path):
    path = tmp_path / "임시 발표.PPTX"
    path.write_bytes(b"saved copy; ZIP validation belongs to A-02")
    return path


@pytest.fixture
def snapshot():
    return DeckSnapshot(
        source_path="original.pptx",
        source_sha256="saved-version-hash",
        slide_count=1,
        width_pt=720.0,
        height_pt=540.0,
        shapes=(
            Shape(
                slide_number=1,
                shape_id=7,
                name="title",
                box=Box(1.0, 2.0, 3.0, 4.0),
                text="발표",
                runs=(TextRun("발표", font_size_pt=24.0, color=(0, 0, 0), bold=True),),
                supported=True,
            ),
        ),
    )


def reader(**options):
    return session.PowerPointReadSession(retry_delay_seconds=0, **options)


def test_read_copy_owns_only_its_document_and_returns_detached_data(runtime, temporary_copy, snapshot):
    original = temporary_copy.read_bytes()

    def extract(presentation):
        assert presentation is runtime.presentation
        runtime.record("extract")
        return snapshot

    result = reader().read_copy(temporary_copy, extract)

    assert result == snapshot
    assert result is not snapshot
    assert result.shapes[0] is not snapshot.shapes[0]
    assert asdict(result)["shapes"][0]["runs"][0]["text"] == "발표"
    assert runtime.open_arguments == [
        (str(temporary_copy.resolve()), {"ReadOnly": -1, "Untitled": -1, "WithWindow": 0})
    ]
    assert runtime.events == [
        "initialize",
        "pump",
        "connect",
        "pump",
        "open",
        "pump",
        "extract",
        "close",
        "uninitialize",
    ]
    assert runtime.presentation.closed
    assert not runtime.user_presentation.closed
    assert runtime.application.Visible == -1
    assert runtime.application.DisplayAlerts == 2
    assert temporary_copy.read_bytes() == original


def test_com_lifecycle_stays_on_the_calling_worker_thread(runtime, temporary_copy, snapshot):
    result = []

    def work():
        result.append(reader().read_copy(temporary_copy, lambda _: snapshot))

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result == [snapshot]
    assert set(runtime.thread_ids) == {worker.ident}
    assert worker.ident != threading.get_ident()


@pytest.mark.parametrize("hresult", [0x80040154, 0x800401F3, -2147221164])
def test_missing_powerpoint_is_distinct_from_connection_failure(runtime, temporary_copy, snapshot, hresult):
    runtime.failures["connect"].append(FakeComError(hresult))

    with pytest.raises(PowerPointNotInstalledError) as caught:
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.hresult == hresult & 0xFFFFFFFF
    assert runtime.events.count("connect") == 1
    assert "open" not in runtime.events
    assert "close" not in runtime.events
    assert runtime.events[-1] == "uninitialize"
    assert not runtime.user_presentation.closed


def test_connection_failure_is_not_retried(runtime, temporary_copy, snapshot):
    runtime.failures["connect"].append(FakeComError(0x80080005))

    with pytest.raises(PowerPointConnectionError) as caught:
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.operation == "connect"
    assert caught.value.attempts == 1
    assert runtime.events.count("connect") == 1
    assert runtime.events[-1] == "uninitialize"


def test_failed_initialization_is_not_uninitialized(runtime, temporary_copy, snapshot):
    runtime.failures["initialize"].append(FakeComError(0x80010106))  # incompatible apartment

    with pytest.raises(PowerPointConnectionError) as caught:
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.operation == "initialize"
    assert runtime.events == ["initialize"]


def test_open_failure_never_closes_an_existing_user_document(runtime, temporary_copy, snapshot):
    runtime.failures["open"].append(FakeComError(0x80070005))

    with pytest.raises(PowerPointOpenError):
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert runtime.events.count("open") == 1
    assert "close" not in runtime.events
    assert runtime.events[-1] == "uninitialize"
    assert not runtime.user_presentation.closed


def test_no_document_returned_is_an_open_failure(runtime, temporary_copy, snapshot):
    runtime.presentation = None

    with pytest.raises(PowerPointOpenError):
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert "close" not in runtime.events
    assert runtime.events[-1] == "uninitialize"


def test_extractor_failure_closes_copy_and_does_not_chain_com_traceback(runtime, temporary_copy):
    def extract(presentation):
        child_proxy = presentation
        assert child_proxy is runtime.presentation
        raise ValueError("unsupported document content")

    with pytest.raises(PowerPointExtractionError, match="unsupported document content") as caught:
        reader().read_copy(temporary_copy, extract)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert runtime.events[-2:] == ["close", "uninitialize"]
    assert runtime.presentation.closed


@pytest.mark.parametrize("failure", [None, ValueError, KeyboardInterrupt])
def test_proxies_are_released_before_com_uninitialization(monkeypatch, temporary_copy, snapshot, failure):
    references = []
    events = []

    class Proxy:
        def __init__(self):
            references.append(weakref.ref(self))

        @property
        def Presentations(self):
            return self

        def Open(self, *args, **kwargs):
            return Proxy()

        def Close(self):
            events.append("close")

    def uninitialize():
        assert all(reference() is None for reference in references)
        events.append("uninitialize")

    runtime = SimpleNamespace(
        com_error=FakeComError,
        initialize=lambda: None,
        connect=Proxy,
        pump_messages=lambda: None,
        uninitialize=uninitialize,
    )
    monkeypatch.setattr(session, "_load_runtime", lambda: runtime)

    def extract(presentation):
        child_proxy = Proxy()
        assert child_proxy is not presentation
        if failure:
            raise failure("interrupted extraction")
        return snapshot

    if failure:
        expected = KeyboardInterrupt if failure is KeyboardInterrupt else PowerPointExtractionError
        with pytest.raises(expected):
            reader().read_copy(temporary_copy, extract)
    else:
        assert reader().read_copy(temporary_copy, extract) == snapshot
    assert events == ["close", "uninitialize"]


def test_the_same_session_can_read_again_after_failure(runtime, temporary_copy, snapshot):
    runtime.failures["open"].append(FakeComError(0x80004005))
    read_session = reader()

    with pytest.raises(PowerPointOpenError):
        read_session.read_copy(temporary_copy, lambda _: snapshot)
    assert read_session.read_copy(temporary_copy, lambda _: snapshot) == snapshot

    assert runtime.events.count("initialize") == 2
    assert runtime.events.count("uninitialize") == 2
    assert runtime.events.count("close") == 1


@pytest.mark.parametrize("operation", ["connect", "open", "close"])
@pytest.mark.parametrize("hresult", [0x80010001, 0x8001010A, -2147418111])
def test_only_known_busy_calls_are_retried(runtime, temporary_copy, snapshot, operation, hresult):
    runtime.failures[operation].extend([FakeComError(hresult), FakeComError(hresult)])

    assert reader().read_copy(temporary_copy, lambda _: snapshot) == snapshot

    assert runtime.events.count(operation) == 3
    assert runtime.events[-1] == "uninitialize"
    assert runtime.presentation.closed
    positions = [i for i, event in enumerate(runtime.events) if event == operation]
    for previous, current in zip(positions, positions[1:], strict=False):
        assert "pump" in runtime.events[previous + 1 : current]


@pytest.mark.parametrize("operation", ["connect", "open", "close"])
def test_busy_attempts_are_bounded(runtime, temporary_copy, snapshot, operation):
    runtime.failures[operation].extend(FakeComError(0x8001010A) for _ in range(4))

    with pytest.raises(PowerPointBusyError) as caught:
        reader(max_attempts=2).read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.operation == operation
    assert caught.value.attempts == 2
    assert runtime.events.count(operation) == 2
    assert runtime.events[-1] == "uninitialize"
    assert not runtime.user_presentation.closed


def test_extractor_is_not_replayed_when_busy(runtime, temporary_copy):
    def extract(_):
        runtime.record("extract")
        raise FakeComError(0x8001010A)

    with pytest.raises(PowerPointBusyError) as caught:
        reader().read_copy(temporary_copy, extract)

    assert caught.value.operation == "extract"
    assert caught.value.attempts == 1
    assert runtime.events.count("extract") == 1
    assert runtime.events[-2:] == ["close", "uninitialize"]


@pytest.mark.parametrize("operation", ["close", "uninitialize"])
def test_cleanup_failure_prevents_a_successful_result(runtime, temporary_copy, snapshot, operation):
    runtime.failures[operation].append(FakeComError(0x80004005))

    with pytest.raises(PowerPointCleanupError) as caught:
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.operation == operation
    assert runtime.events.count(operation) == 1
    assert runtime.events[-1] == "uninitialize"


def test_extraction_error_is_preserved_alongside_both_cleanup_errors(runtime, temporary_copy):
    runtime.failures["close"].append(FakeComError(0x80004005))
    runtime.failures["uninitialize"].append(FakeComError(0x80004005))

    def extract(_):
        raise ValueError("extraction failure")

    with pytest.raises(PowerPointExtractionError) as caught:
        reader().read_copy(temporary_copy, extract)

    assert "extraction failure" in str(caught.value)
    assert [error.operation for error in caught.value.cleanup_errors] == ["close", "uninitialize"]
    assert len(caught.value.__notes__) == 2
    assert runtime.events[-2:] == ["close", "uninitialize"]


def test_both_cleanup_errors_are_reported_after_successful_extraction(runtime, temporary_copy, snapshot):
    runtime.failures["close"].append(FakeComError(0x80004005))
    runtime.failures["uninitialize"].append(FakeComError(0x80004005))

    with pytest.raises(PowerPointCleanupError) as caught:
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert caught.value.operation == "close"
    assert [error.operation for error in caught.value.cleanup_errors] == ["uninitialize"]


@pytest.mark.parametrize("location", ["result", "field", "nested"])
def test_com_objects_cannot_leave_in_the_result(runtime, temporary_copy, snapshot, location):
    def extract(presentation):
        if location == "result":
            return presentation
        if location == "field":
            return replace(snapshot, limitations=(presentation,))
        return replace(snapshot, shapes=(replace(snapshot.shapes[0], box=presentation),))

    with pytest.raises(PowerPointExtractionError):
        reader().read_copy(temporary_copy, extract)

    assert runtime.events[-2:] == ["close", "uninitialize"]


def test_pump_failure_still_cleans_up_the_opened_document(runtime, temporary_copy, snapshot):
    runtime.failures["pump"].extend([None, None, FakeComError(0x80004005)])

    with pytest.raises(PowerPointExtractionError):
        reader().read_copy(temporary_copy, lambda _: snapshot)

    assert runtime.events[-2:] == ["close", "uninitialize"]


def test_wait_keeps_pumping_messages(runtime, monkeypatch):
    times = iter([0.0, 0.0, 0.01, 0.02, 0.03])
    sleeps = []
    monkeypatch.setattr(session.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(session.time, "sleep", sleeps.append)

    session.PowerPointReadSession(retry_delay_seconds=0.03)._wait(runtime, "connect")

    assert runtime.events == ["pump"] * 4
    assert sleeps == pytest.approx([0.01, 0.01, 0.01])


@pytest.mark.parametrize("attempts", [0, -1, 1.5, True])
def test_invalid_attempt_limit_is_rejected(attempts):
    with pytest.raises(ValueError):
        reader(max_attempts=attempts)


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf")])
def test_invalid_retry_delay_is_rejected(delay):
    with pytest.raises(ValueError):
        session.PowerPointReadSession(retry_delay_seconds=delay)


def test_other_file_formats_are_rejected_before_com(runtime, tmp_path, snapshot):
    with pytest.raises(PowerPointOpenError):
        reader().read_copy(tmp_path / "deck.pptm", lambda _: snapshot)
    assert runtime.events == []


def test_non_windows_runtime_has_a_clear_failure(monkeypatch, temporary_copy, snapshot):
    monkeypatch.setattr(session.sys, "platform", "linux")
    with pytest.raises(PowerPointUnavailableError, match="requires Windows"):
        reader().read_copy(temporary_copy, lambda _: snapshot)


def test_missing_pywin32_has_a_clear_failure(monkeypatch, temporary_copy, snapshot):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "pythoncom":
            raise ImportError("pywin32 missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(session.sys, "platform", "win32")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(PowerPointUnavailableError, match="pywin32 missing"):
        reader().read_copy(temporary_copy, lambda _: snapshot)


def test_windows_runtime_uses_sta_and_never_attaches_to_active_object(monkeypatch, temporary_copy, snapshot):
    runtime = FakeRuntime()
    pythoncom = SimpleNamespace(
        COINIT_APARTMENTTHREADED=2,
        com_error=FakeComError,
        CoUninitialize=runtime.uninitialize,
        PumpWaitingMessages=runtime.pump_messages,
    )

    def initialize(flags):
        assert flags == 2
        runtime.initialize()

    def dispatch(progid):
        assert progid == "PowerPoint.Application"
        return runtime.connect()

    pythoncom.CoInitializeEx = initialize
    client = SimpleNamespace(DispatchEx=dispatch)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(session.sys, "platform", "win32")

    assert reader().read_copy(temporary_copy, lambda _: snapshot) == snapshot
    assert runtime.events[0] == "initialize"
    assert runtime.events[-1] == "uninitialize"


def test_common_imports_do_not_require_windows_or_pywin32():
    script = """
import builtins
import sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'pythoncom' or name.startswith('win32com'):
        raise AssertionError('COM libraries imported outside the Windows runtime')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
sys.platform = 'linux'
import slideguard.domain.models
import slideguard.checks.contrast
import slideguard.adapters.powerpoint.reader
import slideguard.adapters.powerpoint.session
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
