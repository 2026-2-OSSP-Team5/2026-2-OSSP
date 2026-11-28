import queue
import threading
from dataclasses import asdict
from hashlib import sha256

import pytest

from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.powerpoint.session import PowerPointReadSession
from slideguard.adapters.watching.catalog import SavedFileCatalog
from slideguard.adapters.watching.monitor import SavedFileMonitor
from slideguard.application.collect import SnapshotCollector
from slideguard.application.ports import FileRemoved, SavedRevision
from slideguard.bootstrap import SnapshotCollection, build_snapshot_collection
from tests.fixtures.presentations import make_deck

pytestmark = [pytest.mark.powerpoint, pytest.mark.usefixtures("powerpoint_environment")]
TIMEOUT = 30
SETTLE = 0.15


@pytest.fixture
def collection(tmp_path):
    pipelines, releases = [], []
    folder = tmp_path / "한글 저장 폴더"
    folder.mkdir()

    def start(*, reader=None, consumer=None, max_attempts=3):
        output, failures, events = queue.Queue(), queue.Queue(), queue.Queue()
        if reader is None:
            pipeline = build_snapshot_collection(
                folder,
                consumer or output.put,
                on_failure=failures.put,
                settle_seconds=SETTLE,
                max_attempts=max_attempts,
                retry_delay_seconds=0.1,
            )
        else:
            collector = SnapshotCollector(
                reader,
                SavedFileCatalog(folder),
                consumer or output.put,
                on_failure=failures.put,
                max_attempts=max_attempts,
                retry_delay_seconds=0.1,
            )
            pipeline = SnapshotCollection(
                SavedFileMonitor(folder, on_event=collector.on_event, settle_seconds=SETTLE), collector
            )
        original = pipeline.monitor.on_event

        def record(event):
            original(event)
            events.put(event)

        pipeline.monitor.on_event = record
        pipelines.append(pipeline)
        pipeline.start()
        assert pipeline.collector.wait_idle(TIMEOUT)
        return pipeline, output, failures, events

    def gate():
        entered, release = threading.Event(), threading.Event()
        releases.append(release)
        return entered, release

    start.folder, start.gate = folder, gate
    yield start
    for release in releases:
        release.set()
    for pipeline in pipelines:
        pipeline.stop()
        assert not pipeline.is_running


def no_delivery(output):
    # Longer than both the quiet interval and monitor polling interval.
    with pytest.raises(queue.Empty):
        output.get(timeout=0.6)


def assert_revision(deck, fixture, path):
    assert deck.source_path == str(path.resolve())
    assert deck.source_sha256 == fixture.sha256
    assert deck.slide_count == 3
    assert deck.width_pt == pytest.approx(960)
    assert deck.height_pt == pytest.approx(540)
    top_level = [s for s in deck.shapes if s.locator.kind == "shape" and len(s.locator.shape_path) == 1]
    assert {(s.slide_number, s.shape_id, s.name) for s in top_level} == set(fixture.shapes)
    assert all(s.slide_number != 2 for s in deck.shapes)
    by_name = {shape.name: shape for shape in deck.shapes}
    basic = by_name["basic-text"]
    assert basic.supported and basic.text == fixture.text
    assert asdict(basic.box) == pytest.approx(dict(x=72, y=72, width=400, height=90))
    assert [(r.text, r.font_size_pt, r.color, r.bold) for r in basic.runs] == [
        (fixture.text, 24, (12, 34, 56), False)
    ]
    mixed = by_name["mixed-text"]
    assert mixed.supported and mixed.text == "작은 글자 큰 글자"
    assert [(r.text, r.font_size_pt, r.color, r.bold) for r in mixed.runs] == [
        ("작은 글자 ", 18, (12, 34, 56), False),
        ("큰 글자", 30, (170, 40, 80), True),
    ]
    for text in (basic, mixed):
        assert text.rendered_box is not None
        assert text.rendered_box.width > 0 and text.rendered_box.height > 0
    for kind in ("table", "picture", "group"):
        unsupported = by_name[f"unsupported-{kind}"]
        assert not unsupported.supported and unsupported.text == "" and unsupported.runs == ()
        label = "Unsupported top-level" if kind == "picture" else "Container"
        assert any(f"{label} shape type: {kind}" in note for note in unsupported.notes)
    assert any(s.locator.kind == "table_cell" and s.text == "표 내부는 순회하지 않음" for s in deck.shapes)
    assert any(len(s.locator.shape_path) == 2 and s.text == "그룹 내부는 순회하지 않음" for s in deck.shapes)
    assert deck.limitations


def test_saved_snapshot_contents_and_source_preservation(collection):
    fixture = make_deck()
    pipeline, output, failures, events = collection()
    path = collection.folder / "발표 자료.PPTX"
    path.write_bytes(fixture.data[: len(fixture.data) // 2])
    no_delivery(output)
    assert events.empty()
    path.write_bytes(fixture.data)
    before = path.stat().st_mtime_ns
    event = events.get(timeout=TIMEOUT)
    assert event == SavedRevision(path, fixture.sha256, len(fixture.data))
    deck = output.get(timeout=TIMEOUT)
    assert_revision(deck, fixture, path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    no_delivery(output)
    assert failures.empty()
    assert path.read_bytes() == fixture.data and path.stat().st_mtime_ns == before


@pytest.mark.parametrize("pattern", ["burst", "atomic-replace", "identical"])
def test_save_patterns_deliver_only_changed_latest_revision(collection, pattern):
    first, middle, latest = (make_deck(label) for label in ("처음", "중간", "최신"))
    pipeline, output, failures, events = collection()
    path = collection.folder / "연속 저장.pptx"
    path.write_bytes(first.data)
    assert output.get(timeout=TIMEOUT).source_sha256 == first.sha256
    assert events.get(timeout=TIMEOUT).source_sha256 == first.sha256
    assert pipeline.collector.wait_idle(TIMEOUT)
    if pattern == "atomic-replace":
        temporary = collection.folder / "office-save.tmp"
        temporary.write_bytes(latest.data)
        temporary.replace(path)
    elif pattern == "burst":
        path.write_bytes(middle.data)
        path.write_bytes(latest.data)
    else:
        path.write_bytes(first.data)
        no_delivery(output)
        assert events.empty() and failures.empty()
        return
    event = events.get(timeout=TIMEOUT)
    assert event.source_sha256 == latest.sha256
    assert_revision(output.get(timeout=TIMEOUT), latest, path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    no_delivery(output)
    assert events.empty() and failures.empty()
    assert path.read_bytes() == latest.data


@pytest.mark.parametrize("change", ["resave", "remove", "stop"])
def test_change_while_real_com_result_is_pending_discards_old_result(collection, change):
    first, latest = make_deck("추출 중 이전 버전"), make_deck("추출 중 최신 버전")
    entered, release = collection.gate()

    class PausedSession:
        def __init__(self):
            self.session = PowerPointReadSession()
            self.calls = 0

        def read_copy(self, path, extract):
            def controlled(presentation):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    assert release.wait(TIMEOUT)
                return extract(presentation)

            return self.session.read_copy(path, controlled)

    session = PausedSession()
    pipeline, output, failures, events = collection(reader=PowerPointDeckReader(session))
    path = collection.folder / "수집 중 변경.pptx"
    path.write_bytes(first.data)
    assert events.get(timeout=TIMEOUT).source_sha256 == first.sha256
    assert entered.wait(TIMEOUT)
    if change == "resave":
        path.write_bytes(latest.data)
        assert events.get(timeout=TIMEOUT).source_sha256 == latest.sha256
        assert pipeline.monitor.is_running
    elif change == "remove":
        path.unlink()
        assert events.get(timeout=TIMEOUT) == FileRemoved(path)
    else:
        pipeline.collector.stop(wait=False)
    release.set()
    assert pipeline.collector.wait_idle(TIMEOUT)
    if change == "resave":
        assert_revision(output.get(timeout=TIMEOUT), latest, path)
        assert session.calls == 2
    else:
        assert session.calls == 1
    no_delivery(output)
    assert failures.empty()


class FailingExtractionSession:
    """Inject an extractor failure after a real COM open, retaining real cleanup."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0
        self.session = PowerPointReadSession()

    def read_copy(self, path, extract):
        def controlled(presentation):
            self.calls += 1
            if self.calls <= self.failures:
                raise RuntimeError("injected extraction failure after real COM open")
            return extract(presentation)

        return self.session.read_copy(path, controlled)


@pytest.mark.parametrize("stage", ["read", "deliver"])
def test_real_com_read_and_consumer_failures_retry_without_stopping_watch(collection, stage):
    fixture = make_deck("실패 후 복구")
    session = FailingExtractionSession(1 if stage == "read" else 0)
    accepted = queue.Queue()
    attempts = []

    def consume(deck):
        attempts.append(deck.source_sha256)
        if stage == "deliver" and len(attempts) == 1:
            raise RuntimeError("injected consumer failure")
        accepted.put(deck)

    pipeline, _, failures, events = collection(reader=PowerPointDeckReader(session), consumer=consume)
    path = collection.folder / "복구.pptx"
    path.write_bytes(fixture.data)
    event = events.get(timeout=TIMEOUT)
    failure = failures.get(timeout=TIMEOUT)
    assert failure.stage == stage and failure.attempt == 1 and failure.retrying
    assert failure.expected_sha256 == event.source_sha256 == fixture.sha256
    assert failure.error_type == ("PowerPointExtractionError" if stage == "read" else "RuntimeError")
    assert_revision(accepted.get(timeout=TIMEOUT), fixture, path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    assert session.calls == 2 and pipeline.is_running
    no_delivery(accepted)
    assert failures.empty()
    assert path.read_bytes() == fixture.data


def assert_editing_document(editor):
    assert editor.application.Version
    assert editor.document.FullName == str(editor.path)
    assert editor.document.Saved == 0
    assert editor.document.Windows.Count > 0
    assert editor.document.Slides.Item(1).Shapes.Item(1).TextFrame.TextRange.Text == "미저장 사용자 편집 보존"
    assert editor.path.read_bytes() == editor.saved_bytes


def test_open_unsaved_document_survives_success_failure_stop_and_explicit_retry(collection, editing_document):
    fixture = make_deck("사용자 문서 보존 검사")
    session = FailingExtractionSession(0)
    pipeline, output, failures, _ = collection(reader=PowerPointDeckReader(session), max_attempts=2)
    path = collection.folder / "별도 검사 대상.pptx"
    path.write_bytes(fixture.data)
    assert_revision(output.get(timeout=TIMEOUT), fixture, path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    assert_editing_document(editing_document)
    session.failures = session.calls + 2
    pipeline.collector.request(path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    assert [(failures.get_nowait().attempt) for _ in range(2)] == [1, 2]
    assert failures.empty() and output.empty()
    assert_editing_document(editing_document)
    pipeline.collector.request(path)  # Same bytes after exhausting retries.
    assert_revision(output.get(timeout=TIMEOUT), fixture, path)
    assert pipeline.collector.wait_idle(TIMEOUT)
    pipeline.stop()
    assert not pipeline.is_running
    assert_editing_document(editing_document)
    assert path.read_bytes() == fixture.data


def test_powerpoint_save_emits_the_office_saved_revision(editing_document):
    editor = editing_document
    output, failures, events = queue.Queue(), queue.Queue(), queue.Queue()
    pipeline = build_snapshot_collection(
        editor.path.parent, output.put, on_failure=failures.put, settle_seconds=SETTLE
    )
    original = pipeline.monitor.on_event

    def record(event):
        original(event)
        events.put(event)

    pipeline.monitor.on_event = record
    pipeline.start()
    try:
        # Unsaved in-memory edits must not be substituted for bytes on disk.
        before = output.get(timeout=TIMEOUT)
        assert before.source_sha256 == sha256(editor.saved_bytes).hexdigest()
        assert "미저장 사용자 편집 보존" not in before.shapes[0].text
        assert pipeline.collector.wait_idle(TIMEOUT)
        editor.document.Save()  # Actual Office save, not a generated ZIP replacement.
        saved_bytes = editor.path.read_bytes()
        saved_mtime = editor.path.stat().st_mtime_ns
        digest = sha256(saved_bytes).hexdigest()
        assert digest != before.source_sha256
        assert events.get(timeout=TIMEOUT) == SavedRevision(editor.path, digest, len(saved_bytes))
        after = output.get(timeout=TIMEOUT)
        assert after.source_path == str(editor.path)
        assert after.source_sha256 == digest
        assert after.shapes[0].text == "미저장 사용자 편집 보존"
        assert after.shapes[0].visible_object_box is not None
        assert editor.document.Saved == -1 and editor.document.Windows.Count > 0
        assert pipeline.collector.wait_idle(TIMEOUT)
        no_delivery(output)
        assert failures.empty()
        assert editor.path.read_bytes() == saved_bytes
        assert editor.path.stat().st_mtime_ns == saved_mtime
    finally:
        pipeline.stop()


def test_shutdown_and_new_session_collect_existing_latest_file(collection):
    first, latest = make_deck("종료 전"), make_deck("재시작 전")
    pipeline, output, failures, _ = collection()
    path = collection.folder / "재시작.pptx"
    path.write_bytes(first.data)
    assert output.get(timeout=TIMEOUT).source_sha256 == first.sha256
    assert pipeline.collector.wait_idle(TIMEOUT)
    pipeline.stop()
    assert not pipeline.is_running
    path.write_bytes(latest.data)
    no_delivery(output)
    restarted, new_output, new_failures, new_events = collection()
    assert_revision(new_output.get(timeout=TIMEOUT), latest, path)
    assert restarted.is_running and new_events.empty()
    no_delivery(new_output)
    assert failures.empty() and new_failures.empty()
