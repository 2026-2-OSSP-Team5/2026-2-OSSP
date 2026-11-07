import ast
import io
import queue
import threading
import weakref
import zipfile
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation

from slideguard import bootstrap
from slideguard.adapters.powerpoint import session
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.adapters.watching.catalog import SavedFileCatalog
from slideguard.application import collect
from slideguard.application.collect import SnapshotCollector
from slideguard.application.ports import FileRemoved, SavedRevision
from slideguard.domain.models import DeckSnapshot
from slideguard.domain.policy import AnalysisPolicy


def revision(path, text="first"):
    return SavedRevision(path, sha256(text.encode()).hexdigest(), len(text))


def snapshot(version):
    return DeckSnapshot(str(version.source_path), version.source_sha256, 1, 960, 540)


class Source:
    def __init__(self, *versions):
        self.versions = {v.source_path: v for v in versions}
        self.listed = tuple(self.versions)
        self.calls = []

    def list_paths(self):
        self.calls.append(("list", threading.get_ident()))
        return self.listed

    def current_revision(self, path):
        self.calls.append(("version", threading.get_ident()))
        return self.versions.get(path)


class Reader:
    def __init__(self, source):
        self.source = source
        self.calls = []
        self.on_read = lambda path, deck: deck

    def read(self, path):
        version = self.source.versions[path]
        self.calls.append((version, threading.get_ident()))
        return self.on_read(path, snapshot(version))


class History:
    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def last_successful_sha256(self, path):
        self.calls.append(path)
        return self.values.get(path)


@pytest.fixture
def make_collector():
    collectors, gates = [], []

    def make(source, *, reader=None, consumer=None, **kwargs):
        output, failures = [], []
        reader = reader or Reader(source)
        collector = SnapshotCollector(
            reader,
            source,
            consumer if consumer is not None else output.append,
            on_failure=kwargs.pop("on_failure", failures.append),
            retry_delay_seconds=kwargs.pop("retry_delay_seconds", 0),
            **kwargs,
        )
        collectors.append(collector)
        return SimpleNamespace(
            collector=collector, reader=reader, source=source, output=output, failures=failures
        )

    def gate():
        entered, release = threading.Event(), threading.Event()
        gates.append(release)
        return entered, release

    make.gate = gate
    yield make
    for release in gates:
        release.set()
    for collector in collectors:
        collector.stop()


def run_idle(case):
    case.collector.start()
    assert case.collector.wait_idle(5)


@pytest.mark.parametrize("history_kind", ["unconnected", "empty", "same", "changed"])
def test_startup_uses_only_successful_history_without_updating_it(tmp_path, make_collector, history_kind):
    version = revision(tmp_path / "deck.pptx")
    values = {} if history_kind == "empty" else {version.source_path: version.source_sha256}
    if history_kind == "changed":
        values[version.source_path] = "previous successful check"
    history = None if history_kind == "unconnected" else History(values)
    case = make_collector(Source(version), successful_hashes=history)
    run_idle(case)
    assert case.output == ([] if history_kind == "same" else [snapshot(version)])
    if history is not None:
        assert history.values == values
    # Explicit same-version requests always bypass the startup filter.
    case.collector.request(version.source_path)
    assert case.collector.wait_idle(5)
    assert case.output[-1] == snapshot(version)
    if history is not None:
        assert history.calls == [version.source_path]
        assert history.values == values


def test_queued_saves_coalesce_and_all_ports_run_serially_off_caller(tmp_path, make_collector):
    path = tmp_path / "deck.pptx"
    versions = [revision(path, str(i)) for i in range(5)]
    case = make_collector(Source(versions[-1]))
    for version in versions:
        case.collector.on_event(version)
    run_idle(case)
    assert case.output == [snapshot(versions[-1])]
    ids = {tid for _, tid in case.source.calls + case.reader.calls}
    assert len(ids) == 1 and threading.get_ident() not in ids


@pytest.mark.parametrize("action", ["save", "remove", "remove-recreate", "request", "stop"])
def test_inflight_generations_drop_obsolete_results_and_callbacks_stay_fast(tmp_path, make_collector, action):
    path = tmp_path / "deck.pptx"
    first, latest = revision(path), revision(path, "latest")
    case = make_collector(Source(first))
    entered, release = make_collector.gate()

    def read(path, deck):
        if len(case.reader.calls) == 1:
            entered.set()
            assert release.wait(5)
        return deck

    case.reader.on_read = read
    case.collector.start()
    assert entered.wait(5)
    # These synchronous calls must return while the reader remains blocked.
    if action == "save":
        for text in ("middle", "latest"):
            current = revision(path, text)
            case.source.versions[path] = current
            case.collector.on_event(current)
    elif action in {"remove", "remove-recreate"}:
        case.collector.on_event(FileRemoved(path))
        if action == "remove-recreate":
            case.collector.on_event(first)
    elif action == "request":
        case.collector.request(path)
    else:
        case.collector.stop(wait=False)
    assert not case.collector.wait_idle(0)
    release.set()
    assert case.collector.wait_idle(5)
    expected = [] if action in {"remove", "stop"} else [snapshot(latest if action == "save" else first)]
    assert case.output == expected
    assert len(case.reader.calls) == (1 if action in {"remove", "stop"} else 2)
    assert case.failures == []


@pytest.mark.parametrize("when", ["before-read", "during-read", "history-skip"])
def test_version_checks_discover_resave_even_before_watcher_event(tmp_path, make_collector, when):
    path = tmp_path / "deck.pptx"
    first, latest = revision(path), revision(path, "latest")
    source = Source(latest if when == "before-read" else first)
    history = History({path: first.source_sha256})
    case = make_collector(source, successful_hashes=history if when == "history-skip" else None)
    if when == "before-read":
        case.collector.on_event(first)
    elif when == "during-read":

        def resave(path, deck):
            source.versions[path] = latest
            return deck

        case.reader.on_read = resave
    else:

        def old_history(path):
            source.versions[path] = latest
            return first.source_sha256

        history.last_successful_sha256 = old_history
    run_idle(case)
    assert case.output == [snapshot(latest)]
    assert case.reader.calls[-1][0] == latest
    assert len(case.reader.calls) == (2 if when == "during-read" else 1)


@pytest.mark.parametrize("part", ["read", "deliver", "version", "history"])
def test_failures_retry_boundedly_and_allow_same_version_request(tmp_path, make_collector, part):
    version = revision(tmp_path / "deck.pptx")
    source = Source(version)
    history = History({})
    delivered = []
    attempts = []

    def fail(*args):
        attempts.append(args)
        raise RuntimeError("temporarily unavailable")

    case = make_collector(source, consumer=delivered.append, successful_hashes=history, max_attempts=2)
    target, name = {
        "read": (case.reader, "on_read"),
        "deliver": (case.collector, "_on_snapshot"),
        "version": (source, "current_revision"),
        "history": (history, "last_successful_sha256"),
    }[part]
    original = getattr(target, name)
    setattr(target, name, fail)
    run_idle(case)
    assert len(attempts) == 2
    assert delivered == []
    assert [f.attempt for f in case.failures] == [1, 2]
    assert [f.retrying for f in case.failures] == [True, False]
    assert {f.stage for f in case.failures} == {part}
    assert all(
        f.error_type == "RuntimeError" and f.message == "temporarily unavailable" for f in case.failures
    )
    assert history.values == {}
    setattr(target, name, original)
    case.collector.request(version.source_path)
    assert case.collector.wait_idle(5)
    assert delivered == [snapshot(version)]
    assert history.values == {}


@pytest.mark.parametrize("wrong", ["hash", "path"])
def test_wrong_reader_snapshot_is_never_relabelled_or_delivered(tmp_path, make_collector, wrong):
    version = revision(tmp_path / "deck.pptx")
    case = make_collector(Source(version), max_attempts=2)
    case.reader.on_read = lambda path, deck: replace(
        deck,
        **({"source_sha256": "wrong"} if wrong == "hash" else {"source_path": str(path.with_stem("other"))}),
    )
    run_idle(case)
    assert case.output == []
    assert len(case.reader.calls) == 2
    assert [f.stage for f in case.failures] == ["verify", "verify"]


@pytest.mark.parametrize("part", ["read", "deliver"])
def test_transient_failure_rechecks_version_before_automatic_retry(tmp_path, make_collector, part):
    path = tmp_path / "deck.pptx"
    first, latest = revision(path), revision(path, "latest")
    case = make_collector(Source(first))
    attempts = []

    def operation(path, deck):
        attempts.append(deck)
        if len(attempts) == 1:
            case.source.versions[path] = latest
            raise RuntimeError("transient failure while resaving")
        return deck

    if part == "read":
        case.reader.on_read = operation
    else:

        def consume(deck):
            operation(path, deck)
            case.output.append(deck)

        case.collector._on_snapshot = consume
    run_idle(case)
    assert case.output == [snapshot(latest)]
    assert len(case.failures) == 1 and case.failures[0].retrying
    assert case.failures[0].expected_sha256 == first.source_sha256


def test_slow_consumer_keeps_events_fast_and_other_files_serial(tmp_path, make_collector):
    first, second = revision(tmp_path / "one.pptx"), revision(tmp_path / "two.pptx")
    case = make_collector(Source(first, second))
    entered, release = make_collector.gate()
    delivered = []

    def consume(deck):
        delivered.append((deck, threading.get_ident()))
        if len(delivered) == 1:
            entered.set()
            assert release.wait(5)

    case.collector._on_snapshot = consume
    case.collector.start()
    assert entered.wait(5)
    changed = revision(second.source_path, "changed")
    case.source.versions[second.source_path] = changed
    case.collector.on_event(changed)
    assert len(case.reader.calls) == 1
    release.set()
    assert case.collector.wait_idle(5)
    assert [deck for deck, _ in delivered] == [snapshot(first), snapshot(changed)]
    ids = {tid for _, tid in case.reader.calls + delivered}
    assert len(ids) == 1 and threading.get_ident() not in ids


def test_removed_inflight_failure_is_not_retried_or_reported(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    case = make_collector(Source(version))
    entered, release = make_collector.gate()

    def read(path, deck):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("obsolete read failure")

    case.reader.on_read = read
    case.collector.start()
    assert entered.wait(5)
    case.collector.on_event(FileRemoved(version.source_path))
    release.set()
    assert case.collector.wait_idle(5)
    assert len(case.reader.calls) == 1
    assert case.output == case.failures == []


def test_missing_version_and_invalid_source_path_are_failures_not_completed_checks(tmp_path, make_collector):
    path = tmp_path / "deck.pptx"
    source = Source()
    source.listed = (path,)
    case = make_collector(source, max_attempts=1)
    run_idle(case)
    assert case.failures[0].error_type == "FileNotFoundError"
    source.versions[path] = revision(path.with_stem("other"))
    case.collector.request(path)
    assert case.collector.wait_idle(5)
    assert case.failures[-1].error_type == "ValueError"
    assert case.output == [] and case.reader.calls == []


@pytest.mark.parametrize("action", ["remove", "save", "request"])
def test_initial_listing_does_not_resurrect_or_overwrite_events(tmp_path, make_collector, action):
    version = revision(tmp_path / "deck.pptx")
    source = Source(version)
    history = History({version.source_path: version.source_sha256})
    entered, release = make_collector.gate()

    def listing():
        entered.set()
        assert release.wait(5)
        return source.listed

    source.list_paths = listing
    case = make_collector(source, successful_hashes=history)
    case.collector.start()
    assert entered.wait(5)
    if action == "remove":
        case.collector.on_event(FileRemoved(version.source_path))
    elif action == "save":
        case.collector.on_event(version)
    else:
        case.collector.request(version.source_path)
    release.set()
    assert case.collector.wait_idle(5)
    assert case.output == ([] if action == "remove" else [snapshot(version)])
    assert history.calls == []


def test_listing_retries_do_not_block_saves_and_stop_cancels_retry(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    source = Source(version)
    attempts = []
    failed = threading.Event()
    delivered = threading.Event()

    def listing():
        attempts.append(1)
        raise OSError("history folder unavailable")

    source.list_paths = listing
    case = make_collector(
        source, retry_delay_seconds=30, on_failure=lambda f: failed.set(), consumer=lambda d: delivered.set()
    )
    case.collector.start()
    assert failed.wait(5)
    case.collector.on_event(version)
    assert delivered.wait(5)
    assert not case.collector.wait_idle(0)
    case.collector.stop()
    assert case.collector.wait_idle(0)
    assert len(attempts) == 1


def test_listing_failure_is_bounded_and_manual_requests_still_work(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    source = Source(version)

    def listing():
        raise OSError("folder unavailable")

    source.list_paths = listing
    case = make_collector(source, max_attempts=2)
    run_idle(case)
    assert len(case.failures) == 2
    assert all(f.stage == "list" and f.source_path is None for f in case.failures)
    case.collector.request(version.source_path)
    assert case.collector.wait_idle(5)
    assert case.output == [snapshot(version)]


@pytest.mark.parametrize("action", ["remove", "replace", "request", "stop"])
def test_pending_retry_is_invalidated_without_waiting_for_timer(tmp_path, make_collector, action):
    path = tmp_path / "deck.pptx"
    version, latest = revision(path), revision(path, "latest")
    failed = threading.Event()
    case = make_collector(Source(version), on_failure=lambda f: failed.set(), retry_delay_seconds=30)

    def read(path, deck):
        raise RuntimeError("read failed")

    case.reader.on_read = read
    case.collector.start()
    assert failed.wait(5)
    case.reader.on_read = lambda path, deck: deck
    if action == "remove":
        case.collector.on_event(FileRemoved(path))
    elif action == "replace":
        case.source.versions[path] = latest
        case.collector.on_event(latest)
    elif action == "request":
        case.collector.request(path)
    else:
        case.collector.stop()
    assert case.collector.wait_idle(5)
    assert case.output == (
        [] if action in {"remove", "stop"} else [snapshot(latest if action == "replace" else version)]
    )


def test_failure_data_does_not_retain_error_frames_and_failure_callback_can_raise(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    refs, failures = [], []

    def read(path, deck):
        proxy = SimpleProxy()
        refs.append(weakref.ref(proxy))
        raise RuntimeError("plain reason")

    def failure(info):
        failures.append(asdict(info))
        raise ValueError("UI disconnected")

    case = make_collector(Source(version), on_failure=failure, max_attempts=1)
    case.reader.on_read = read
    run_idle(case)
    assert refs[0]() is None
    assert set(failures[0]) == {
        "source_path",
        "expected_sha256",
        "stage",
        "error_type",
        "message",
        "attempt",
        "retrying",
    }
    case.reader.on_read = lambda path, deck: deck
    case.collector.request(version.source_path)
    assert case.collector.wait_idle(5)
    assert case.output == [snapshot(version)]


class SimpleProxy:
    pass


def test_reentrant_delivery_request_is_not_lost_and_self_stop_does_not_deadlock(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    delivered = []

    def consume(deck):
        delivered.append(deck)
        with pytest.raises(RuntimeError, match="cannot wait for itself"):
            case.collector.wait_idle(0)
        if len(delivered) == 1:
            case.collector.request(version.source_path)
        else:
            case.collector.stop()

    case = make_collector(Source(version), consumer=consume)
    run_idle(case)
    assert delivered == [snapshot(version), snapshot(version)]
    assert not case.collector.is_running


def test_stop_before_start_and_pending_removal_need_no_port_calls(tmp_path, make_collector):
    version = revision(tmp_path / "deck.pptx")
    case = make_collector(Source())
    case.collector.on_event(version)
    case.collector.on_event(FileRemoved(version.source_path))
    run_idle(case)
    assert case.reader.calls == []
    case.collector.stop()
    case.collector.on_event(version)
    case.collector.request(version.source_path)
    assert case.collector.wait_idle(0)
    with pytest.raises(RuntimeError):
        case.collector.start()
    other = make_collector(Source(version))
    other.collector.stop()
    with pytest.raises(RuntimeError):
        other.collector.start()
    assert other.source.calls == []


def test_collection_has_no_adapter_imports_or_direct_file_access(tmp_path, make_collector, monkeypatch):
    # Parse before patching filesystem methods needed by the test harness itself.
    tree = ast.parse(Path(collect.__file__).read_text(encoding="utf-8"))
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert all(
        not (name or "").startswith(("slideguard.adapters", "watchdog", "win32com", "sqlite3"))
        for name in imports
    )
    version = revision(tmp_path / "deck.pptx")
    case = make_collector(Source(version))

    def forbidden(*args, **kwargs):
        raise AssertionError("application must use its injected file source")

    with monkeypatch.context() as patch:
        for name in ("open", "read_bytes", "stat", "resolve"):
            patch.setattr(Path, name, forbidden)
        run_idle(case)
    assert case.output == [snapshot(version)]


def pptx_bytes(slides=1):
    presentation = Presentation()
    for _ in range(slides):
        presentation.slides.add_slide(presentation.slide_layouts[6])
    stream = io.BytesIO()
    presentation.save(stream)
    return stream.getvalue()


def test_catalog_shares_monitor_scope_and_validates_actual_saved_bytes(tmp_path):
    data = pptx_bytes()
    valid = tmp_path / "발표.PPTX"
    valid.write_bytes(data)
    for name in ("~$lock.pptx", ".hidden.pptx", "~scratch.pptx", "other.ppt", "other.tmp"):
        (tmp_path / name).write_bytes(data)
    nested = tmp_path / "nested"
    nested.mkdir()
    child = nested / "child.pptx"
    child.write_bytes(data)
    catalog = SavedFileCatalog(tmp_path)
    assert catalog.list_paths() == (valid,)
    assert set(SavedFileCatalog(tmp_path, recursive=True).list_paths()) == {valid, child}
    assert catalog.current_revision(valid) == SavedRevision(valid, sha256(data).hexdigest(), len(data))
    valid.unlink()
    assert catalog.current_revision(valid) is None
    for path in (child, tmp_path.parent / "outside.pptx", tmp_path / "~$lock.pptx"):
        with pytest.raises(ValueError, match="scope"):
            catalog.current_revision(path)
    valid.write_bytes(b"incomplete ZIP")
    with pytest.raises(zipfile.BadZipFile):
        catalog.current_revision(valid)
    with pytest.raises(NotADirectoryError):
        SavedFileCatalog(tmp_path / "missing").list_paths()


def test_catalog_rejects_aliases_instead_of_changing_snapshot_identity(tmp_path, monkeypatch):
    path = tmp_path / "alias.pptx"
    catalog = SavedFileCatalog(tmp_path)
    monkeypatch.setattr(Path, "resolve", lambda self, **kw: tmp_path / "actual.pptx")
    with pytest.raises(ValueError, match="Aliased"):
        catalog.current_revision(path)


def test_bootstrap_real_watch_keeps_detecting_while_fake_reader_is_blocked(tmp_path, monkeypatch):
    path = tmp_path / "deck.pptx"
    path.write_bytes(pptx_bytes())
    catalog = SavedFileCatalog(tmp_path)
    first = catalog.current_revision(path)
    entered, release = threading.Event(), threading.Event()
    delivered, detected = queue.Queue(), queue.Queue()
    calls = []

    def read(self, path):
        version = catalog.current_revision(path)
        calls.append(version)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return snapshot(version)

    monkeypatch.setattr(PowerPointDeckReader, "read", read)
    pipeline = bootstrap.build_snapshot_collection(tmp_path, delivered.put, settle_seconds=0.05)
    original = pipeline.monitor.on_event

    def event(info):
        original(info)
        detected.put(info)

    pipeline.monitor.on_event = event
    pipeline.start()
    try:
        assert pipeline.is_running and entered.wait(5)
        path.write_bytes(pptx_bytes(2))
        latest = detected.get(timeout=5)
        assert isinstance(latest, SavedRevision) and latest.source_sha256 != first.source_sha256
        assert pipeline.monitor.is_running and delivered.empty()
        release.set()
        assert delivered.get(timeout=5) == snapshot(latest)
        assert pipeline.collector.wait_idle(5)
        assert delivered.empty()
    finally:
        release.set()
        pipeline.stop()
    assert not pipeline.is_running


def test_com_lifecycle_and_delivery_share_collector_worker(tmp_path, monkeypatch):
    path = tmp_path / "deck.pptx"
    path.write_bytes(pptx_bytes())
    events = []

    def record(operation):
        events.append((operation, threading.get_ident()))

    class Runtime:
        com_error = RuntimeError

        def initialize(self):
            record("initialize")

        def connect(self):
            record("connect")
            return SimpleNamespace(Presentations=SimpleNamespace(Open=self.open))

        def open(self, path, **options):
            record("open")
            return SimpleNamespace(
                Slides=SimpleNamespace(Count=0),
                PageSetup=SimpleNamespace(SlideWidth=960, SlideHeight=540),
                Close=lambda: record("close"),
            )

        def pump_messages(self):
            record("pump")

        def uninitialize(self):
            record("uninitialize")

    monkeypatch.setattr(session, "_load_runtime", Runtime)
    pipeline = bootstrap.build_snapshot_collection(tmp_path, lambda deck: record("deliver"))
    pipeline.start()
    try:
        assert pipeline.collector.wait_idle(5)
    finally:
        pipeline.stop()
    names = [name for name, _ in events]
    assert names[0] == "initialize"
    assert names[-3:] == ["close", "uninitialize", "deliver"]
    assert "pump" in names and "open" in names
    ids = {tid for _, tid in events}
    assert len(ids) == 1 and threading.get_ident() not in ids


def test_bootstrap_start_failure_cleans_up_and_existing_analyzer_stays_ooxml(tmp_path, monkeypatch):
    pipeline = bootstrap.build_snapshot_collection(tmp_path / "missing", lambda deck: None)
    with pytest.raises(NotADirectoryError):
        pipeline.start()
    assert not pipeline.is_running and pipeline.collector.wait_idle(0)
    path = tmp_path / "deck.pptx"
    path.write_bytes(pptx_bytes())

    def forbidden(*args):
        pytest.fail("inspect must keep using the OOXML reader")

    monkeypatch.setattr(session, "_load_runtime", forbidden)
    report = bootstrap.build_analyzer().execute(path, AnalysisPolicy())
    assert report.status == "analyzed"
    assert report.executed_rules == ("contrast", "typography", "layout")
    assert all(finding.severity == "unknown" for finding in report.findings)
    assert report.deck.source_sha256 == sha256(path.read_bytes()).hexdigest()

    database_path = tmp_path / "history.sqlite3"
    SqliteReportRepository(database_path).save(report)
    restored = SqliteReportRepository(database_path).list_recent()[0].report
    assert restored == report


def test_bootstrap_worker_start_failure_preserves_error_and_stops_monitor(tmp_path, monkeypatch):
    start = threading.Thread.start

    def fail_collector(thread):
        if thread.name == "pptx-snapshot-collector":
            raise RuntimeError("cannot create collection worker")
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_collector)
    pipeline = bootstrap.build_snapshot_collection(tmp_path, lambda deck: None)
    with pytest.raises(RuntimeError, match="cannot create collection worker") as caught:
        pipeline.start()
    assert not getattr(caught.value, "__notes__", ())
    assert not pipeline.monitor.is_running
    assert pipeline.collector.wait_idle(0)
