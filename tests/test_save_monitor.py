import io
import logging
import queue
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pptx import Presentation
from pptx.util import Inches

from slideguard.adapters.watching import monitor as watching
from slideguard.adapters.watching import revisions as saved_files
from slideguard.adapters.watching.monitor import SavedFileMonitor
from slideguard.application.ports import FileRemoved, SavedRevision


def pptx_bytes(text: str) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2)).text = text
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def test_baseline_and_identical_bytes_are_not_new_saves(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="slideguard.watch")
    path = tmp_path / "발표.PPTX"
    original = pptx_bytes("initial")
    path.write_bytes(original)
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, settle_seconds=0.15)
    monitor.start()
    try:
        path.write_bytes(original)
        time.sleep(0.6)
        assert saved.empty()
        path.write_bytes(pptx_bytes("changed"))
        assert saved.get(timeout=5) == path
        time.sleep(0.5)
        assert saved.empty()
        assert sum("SAVE_DETECTED" in r.message for r in caplog.records) == 1
    finally:
        monitor.stop()
    assert not monitor.is_running


def test_partial_zip_and_repeated_writes_emit_only_completed_save(tmp_path):
    path = tmp_path / "partial.pptx"
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, settle_seconds=0.15)
    monitor.start()
    try:
        path.write_bytes(b"incomplete ZIP")
        time.sleep(0.5)
        assert saved.empty()
        completed = pptx_bytes("complete")
        with path.open("wb") as stream:
            stream.write(completed[: len(completed) // 2])
            stream.flush()
            time.sleep(0.1)
            assert saved.empty()
            stream.write(completed[len(completed) // 2 :])
        assert saved.get(timeout=5) == path
        time.sleep(0.5)
        assert saved.empty()
    finally:
        monitor.stop()


def test_atomic_replace_save_is_detected(tmp_path):
    path = tmp_path / "atomic.pptx"
    path.write_bytes(pptx_bytes("old"))
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, settle_seconds=0.15)
    monitor.start()
    try:
        temporary = tmp_path / "ppt-save.tmp"
        temporary.write_bytes(pptx_bytes("new"))
        temporary.replace(path)
        assert saved.get(timeout=5) == path
        time.sleep(0.5)
        assert saved.empty()
    finally:
        monitor.stop()


def test_lock_files_and_other_extensions_are_ignored(tmp_path):
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, settle_seconds=0.15)
    monitor.start()
    try:
        data = pptx_bytes("ignore")
        for name in ("~$deck.pptx", "~scratch.pptx", ".temporary.pptx", "deck.tmp", "deck.ppt"):
            (tmp_path / name).write_bytes(data)
        time.sleep(0.6)
        assert saved.empty()
        valid = tmp_path / "deck.pptx"
        valid.write_bytes(data)
        assert saved.get(timeout=5) == valid
    finally:
        monitor.stop()


@pytest.mark.parametrize("recursive", [False, True])
def test_subfolders_follow_recursive_option(tmp_path, recursive):
    nested = tmp_path / "nested"
    nested.mkdir()
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, recursive=recursive, settle_seconds=0.15)
    monitor.start()
    try:
        path = nested / "lecture.pptx"
        path.write_bytes(pptx_bytes("nested"))
        if recursive:
            assert saved.get(timeout=5) == path
        else:
            time.sleep(0.6)
            assert saved.empty()
    finally:
        monitor.stop()


def test_moved_folder_is_scanned_when_recursive(tmp_path):
    watched = tmp_path / "watched"
    incoming = tmp_path / "incoming"
    watched.mkdir()
    incoming.mkdir()
    (incoming / "moved.pptx").write_bytes(pptx_bytes("move folder"))
    saved = queue.Queue()
    monitor = SavedFileMonitor(watched, saved.put, recursive=True, settle_seconds=0.15)
    monitor.start()
    try:
        destination = watched / "incoming"
        incoming.rename(destination)
        assert saved.get(timeout=5) == destination / "moved.pptx"
    finally:
        monitor.stop()


def test_invalid_file_times_out_then_next_write_can_be_detected(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="slideguard.watch")
    saved = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, saved.put, settle_seconds=0.1, retry_seconds=0.3)
    monitor.start()
    try:
        path = tmp_path / "broken.pptx"
        path.write_bytes(b"not a presentation")
        deadline = time.monotonic() + 5
        while not any("SAVE_UNREADABLE" in r.message for r in caplog.records):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert saved.empty()
        path.write_bytes(pptx_bytes("fixed"))
        assert saved.get(timeout=5) == path
    finally:
        monitor.stop()


def test_invalid_directory_is_rejected(tmp_path):
    with pytest.raises(NotADirectoryError):
        SavedFileMonitor(tmp_path / "missing").start()


def test_callback_failure_does_not_stop_following_saves(tmp_path, caplog):
    saved = queue.Queue()

    def consume(path: Path):
        saved.put(path)
        raise RuntimeError("consumer failed")

    monitor = SavedFileMonitor(tmp_path, consume, settle_seconds=0.15)
    monitor.start()
    try:
        path = tmp_path / "callback.pptx"
        path.write_bytes(pptx_bytes("one"))
        assert saved.get(timeout=5) == path
        path.write_bytes(pptx_bytes("two"))
        assert saved.get(timeout=5) == path
        assert monitor.is_running
        assert any("SAVE_CALLBACK_FAILED" in r.message for r in caplog.records)
    finally:
        monitor.stop()


def test_version_event_matches_bytes_and_keeps_the_legacy_callback(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="slideguard.watch")
    events, saved = queue.Queue(), queue.Queue()
    path = tmp_path / "저장 버전.PPTX"
    data = pptx_bytes("버전 메타데이터")
    monitor = SavedFileMonitor(tmp_path, saved.put, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        path.write_bytes(data)
        assert events.get(timeout=5) == SavedRevision(path, sha256(data).hexdigest(), len(data))
        assert saved.get(timeout=5) == path
        assert any(
            f"sha256={sha256(data).hexdigest()} | bytes={len(data)}" in record.message
            for record in caplog.records
            if "SAVE_DETECTED" in record.message
        )
    finally:
        monitor.stop()


def test_baseline_is_not_emitted_but_its_removal_is(tmp_path):
    events, saved = queue.Queue(), queue.Queue()
    path = tmp_path / "existing.pptx"
    path.write_bytes(pptx_bytes("baseline"))
    monitor = SavedFileMonitor(tmp_path, saved.put, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        assert events.empty()
        path.unlink()
        assert events.get(timeout=5) == FileRemoved(path)
        with pytest.raises(queue.Empty):
            events.get(timeout=0.3)
        assert saved.empty()
    finally:
        monitor.stop()


def test_rename_emits_old_path_removal_and_new_path_revision(tmp_path):
    events, saved = queue.Queue(), queue.Queue()
    old, new = tmp_path / "old.pptx", tmp_path / "새 이름.PPTX"
    data = pptx_bytes("renamed")
    old.write_bytes(data)
    monitor = SavedFileMonitor(tmp_path, saved.put, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        old.rename(new)
        actual = {events.get(timeout=5), events.get(timeout=5)}
        assert actual == {FileRemoved(old), SavedRevision(new, sha256(data).hexdigest(), len(data))}
        assert saved.get(timeout=5) == new
        assert saved.empty()
    finally:
        monitor.stop()


@pytest.mark.parametrize("changed", [False, True])
def test_atomic_replacement_never_emits_a_removal(tmp_path, changed):
    events = queue.Queue()
    path = tmp_path / "atomic.pptx"
    original = pptx_bytes("old")
    replacement = pptx_bytes("changed") if changed else original
    path.write_bytes(original)
    monitor = SavedFileMonitor(tmp_path, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        temporary = tmp_path / "replacement.tmp"
        temporary.write_bytes(replacement)
        temporary.replace(path)
        if changed:
            assert events.get(timeout=5) == SavedRevision(
                path, sha256(replacement).hexdigest(), len(replacement)
            )
        with pytest.raises(queue.Empty):
            events.get(timeout=0.5)
        path.unlink()
        assert events.get(timeout=5) == FileRemoved(path)
    finally:
        monitor.stop()


def test_recreation_after_confirmed_removal_is_a_new_lifecycle(tmp_path):
    events = queue.Queue()
    path = tmp_path / "recreated.pptx"
    data = pptx_bytes("same bytes")
    path.write_bytes(data)
    monitor = SavedFileMonitor(tmp_path, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        path.unlink()
        assert events.get(timeout=5) == FileRemoved(path)
        path.write_bytes(data)
        assert events.get(timeout=5) == SavedRevision(path, sha256(data).hexdigest(), len(data))
    finally:
        monitor.stop()


def test_back_to_back_writes_emit_the_final_stable_revision(tmp_path):
    events = queue.Queue()
    path = tmp_path / "continuous.pptx"
    versions = [pptx_bytes(text) for text in ("one", "two", "last saved version")]
    monitor = SavedFileMonitor(tmp_path, on_event=events.put, settle_seconds=0.2)
    monitor.start()
    try:
        for data in versions:
            path.write_bytes(data)
        final = versions[-1]
        assert events.get(timeout=5) == SavedRevision(path, sha256(final).hexdigest(), len(final))
        with pytest.raises(queue.Empty):
            events.get(timeout=0.4)
    finally:
        monitor.stop()


def test_event_callback_failure_does_not_block_legacy_or_future_events(tmp_path, caplog):
    events, saved = queue.Queue(), queue.Queue()
    path = tmp_path / "callbacks.pptx"
    first, second = pptx_bytes("first"), pptx_bytes("second")

    def consume(event):
        events.put(event)
        raise RuntimeError("event consumer failed")

    monitor = SavedFileMonitor(tmp_path, saved.put, on_event=consume, settle_seconds=0.1)
    monitor.start()
    try:
        path.write_bytes(first)
        assert events.get(timeout=5) == SavedRevision(path, sha256(first).hexdigest(), len(first))
        assert saved.get(timeout=5) == path
        path.unlink()
        assert events.get(timeout=5) == FileRemoved(path)
        path.write_bytes(second)
        assert events.get(timeout=5) == SavedRevision(path, sha256(second).hexdigest(), len(second))
        assert saved.get(timeout=5) == path
        assert monitor.is_running
        assert any("EVENT_CALLBACK_FAILED | event=FileRemoved" in record.message for record in caplog.records)
        assert any(
            "EVENT_CALLBACK_FAILED | event=SavedRevision" in record.message for record in caplog.records
        )
    finally:
        monitor.stop()


def test_legacy_callback_failure_does_not_block_version_events(tmp_path, caplog):
    events, saved = queue.Queue(), queue.Queue()
    path = tmp_path / "legacy.pptx"
    versions = [pptx_bytes("first"), pptx_bytes("second")]

    def consume(path):
        saved.put(path)
        raise RuntimeError("legacy consumer failed")

    monitor = SavedFileMonitor(tmp_path, consume, on_event=events.put, settle_seconds=0.1)
    monitor.start()
    try:
        for data in versions:
            path.write_bytes(data)
            assert events.get(timeout=5) == SavedRevision(path, sha256(data).hexdigest(), len(data))
            assert saved.get(timeout=5) == path
        assert monitor.is_running
        assert any("SAVE_CALLBACK_FAILED" in record.message for record in caplog.records)
    finally:
        monitor.stop()


@pytest.mark.parametrize("inside", [False, True])
def test_recursive_folder_move_removes_old_paths_and_only_adds_in_scope_paths(tmp_path, inside):
    watched = tmp_path / "watched"
    nested = watched / "nested"
    nested.mkdir(parents=True)
    old = nested / "deck.pptx"
    data = pptx_bytes("folder move")
    old.write_bytes(data)
    events = queue.Queue()
    monitor = SavedFileMonitor(watched, on_event=events.put, recursive=True, settle_seconds=0.1)
    monitor.start()
    try:
        destination = (watched if inside else tmp_path) / "renamed"
        nested.rename(destination)
        expected = {FileRemoved(old)}
        if inside:
            expected.add(SavedRevision(destination / old.name, sha256(data).hexdigest(), len(data)))
        assert {events.get(timeout=5) for _ in expected} == expected
        with pytest.raises(queue.Empty):
            events.get(timeout=0.3)
    finally:
        monitor.stop()


def test_an_unreadable_baseline_file_still_emits_removal(tmp_path, caplog):
    path = tmp_path / "invalid.pptx"
    path.write_bytes(b"incomplete PPTX")
    events = queue.Queue()
    monitor = SavedFileMonitor(tmp_path, on_event=events.put, settle_seconds=0.1, retry_seconds=0.3)
    monitor.start()
    try:
        deadline = time.monotonic() + 5
        while not any("SAVE_UNREADABLE" in record.message for record in caplog.records):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert events.empty()
        path.unlink()
        assert events.get(timeout=5) == FileRemoved(path)
    finally:
        monitor.stop()


@pytest.fixture
def state_monitor(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(watching, "time", SimpleNamespace(monotonic=lambda: clock.now))
    events = []
    monitor = SavedFileMonitor(tmp_path, on_event=events.append, settle_seconds=1)
    return monitor, clock, events


def check_pending(monitor, path):
    monitor._check_pending(path, monitor._pending[path])


def remember_file(state_monitor, path, data):
    monitor, clock, events = state_monitor
    path.write_bytes(data)
    monitor._enqueue(path)
    clock.now += 1.1
    check_pending(monitor, path)
    assert events.pop() == SavedRevision(path, sha256(data).hexdigest(), len(data))


def test_removal_quiet_interval_starts_at_observed_absence_not_old_event_age(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "late-delete.pptx"
    remember_file(state_monitor, path, pptx_bytes("baseline"))
    monitor._enqueue(path)
    clock.now += 10
    path.unlink()
    check_pending(monitor, path)
    assert events == []
    clock.now += 0.9
    check_pending(monitor, path)
    assert events == []
    clock.now += 0.2
    check_pending(monitor, path)
    assert events == [FileRemoved(path)]


@pytest.mark.parametrize("changed", [False, True])
def test_short_observed_absence_is_a_replacement_not_a_removal(state_monitor, tmp_path, changed):
    monitor, clock, events = state_monitor
    path = tmp_path / "gap.pptx"
    original = pptx_bytes("old")
    replacement = pptx_bytes("new") if changed else original
    remember_file(state_monitor, path, original)
    monitor._enqueue(path)
    path.unlink()
    check_pending(monitor, path)
    clock.now += 0.5
    path.write_bytes(replacement)
    check_pending(monitor, path)
    assert events == []
    clock.now += 1.1
    check_pending(monitor, path)
    expected = [SavedRevision(path, sha256(replacement).hexdigest(), len(replacement))] if changed else []
    assert events == expected


def test_duplicate_delete_notifications_do_not_repeat_removal(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "deleted.pptx"
    remember_file(state_monitor, path, pptx_bytes("old"))
    path.unlink()
    for _ in range(2):
        monitor._enqueue(path)
        check_pending(monitor, path)
        clock.now += 1.1
        check_pending(monitor, path)
    assert events == [FileRemoved(path)]


def test_unknown_missing_paths_do_not_emit_ghost_removals(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "never-seen.pptx"
    monitor._enqueue(path)
    check_pending(monitor, path)
    clock.now += 2
    check_pending(monitor, path)
    assert events == []


def test_known_removal_is_reconciled_without_any_filesystem_event(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "missed-deletion.pptx"
    remember_file(state_monitor, path, pptx_bytes("old"))
    path.unlink()
    monitor._reconcile_missing()
    check_pending(monitor, path)
    clock.now += 1.1
    check_pending(monitor, path)
    monitor._reconcile_missing()
    assert events == [FileRemoved(path)]
    assert path not in monitor._pending


def test_presence_reconciliation_does_not_read_or_rehash_file_bytes(state_monitor, tmp_path, monkeypatch):
    monitor, _, events = state_monitor
    path = tmp_path / "present.pptx"
    remember_file(state_monitor, path, pptx_bytes("old"))

    def no_bytes(*args, **kwargs):
        raise AssertionError("Presence reconciliation must only inspect metadata")

    monkeypatch.setattr(Path, "open", no_bytes)
    monitor._reconcile_missing()
    assert events == []
    assert path not in monitor._pending


def test_newer_pending_delete_prevents_a_stale_removal_commit(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "pending.pptx"
    remember_file(state_monitor, path, pptx_bytes("old"))
    path.unlink()
    monitor._enqueue(path)
    stale = monitor._pending[path]
    check_pending(monitor, path)
    clock.now += 2
    monitor._enqueue(path)
    monitor._check_pending(path, stale)
    assert events == []
    check_pending(monitor, path)
    clock.now += 1.1
    check_pending(monitor, path)
    assert events == [FileRemoved(path)]


def test_access_error_interrupts_the_confirmed_absence_interval(state_monitor, tmp_path, monkeypatch):
    monitor, clock, events = state_monitor
    path = tmp_path / "interrupted-absence.pptx"
    remember_file(state_monitor, path, pptx_bytes("old"))
    path.unlink()
    monitor._enqueue(path)
    check_pending(monitor, path)
    clock.now += 2

    def inaccessible(_):
        raise PermissionError("temporary access denial")

    with monkeypatch.context() as scope:
        scope.setattr(watching, "_signature", inaccessible)
        check_pending(monitor, path)
    check_pending(monitor, path)
    assert events == []
    clock.now += 1.1
    check_pending(monitor, path)
    assert events == [FileRemoved(path)]


def test_newer_event_during_validation_prevents_a_stale_saved_event(state_monitor, tmp_path, monkeypatch):
    monitor, clock, events = state_monitor
    path = tmp_path / "changed-during-validation.pptx"
    first, second = pptx_bytes("first"), pptx_bytes("newer")
    path.write_bytes(first)
    monitor._enqueue(path)
    original = watching._saved_revision

    def validate_then_save_again(path):
        result = original(path)
        path.write_bytes(second)
        monitor._enqueue(path)
        return result

    clock.now += 1.1
    with monkeypatch.context() as scope:
        scope.setattr(watching, "_saved_revision", validate_then_save_again)
        check_pending(monitor, path)
    assert events == []
    clock.now += 1.1
    check_pending(monitor, path)
    assert events == [SavedRevision(path, sha256(second).hexdigest(), len(second))]


def test_callback_failure_does_not_roll_back_detected_hash_or_redeliver_identical_bytes(
    state_monitor, tmp_path
):
    monitor, clock, events = state_monitor
    path = tmp_path / "failed-delivery.pptx"

    def consume(event):
        events.append(event)
        raise RuntimeError("collection retry belongs to the consumer")

    monitor.on_event = consume
    data = pptx_bytes("same bytes")
    path.write_bytes(data)
    for _ in range(2):
        monitor._enqueue(path)
        clock.now += 1.1
        check_pending(monitor, path)
    assert events == [SavedRevision(path, sha256(data).hexdigest(), len(data))]


def test_legacy_callback_can_trigger_a_new_save_without_relabeling_the_old_event(state_monitor, tmp_path):
    monitor, clock, events = state_monitor
    path = tmp_path / "save-in-callback.pptx"
    first, second = pptx_bytes("first"), pptx_bytes("second")

    def save_new_version(path):
        path.write_bytes(second)
        monitor._enqueue(path)

    monitor.on_saved = save_new_version
    path.write_bytes(first)
    monitor._enqueue(path)
    clock.now += 1.1
    check_pending(monitor, path)
    assert events == [SavedRevision(path, sha256(first).hexdigest(), len(first))]
    monitor.on_saved = None
    clock.now += 1.1
    check_pending(monitor, path)
    assert events[-1] == SavedRevision(path, sha256(second).hexdigest(), len(second))


def test_event_paths_are_absolute_lexical_paths_even_after_deletion(state_monitor, tmp_path, monkeypatch):
    monitor, clock, events = state_monitor
    monkeypatch.chdir(tmp_path)
    relative = Path("unused-parent") / ".." / "정규화.PPTX"
    path = tmp_path / "정규화.PPTX"
    data = pptx_bytes("normalized")
    path.write_bytes(data)
    monitor._enqueue(relative)
    clock.now += 1.1
    check_pending(monitor, path)
    assert events.pop() == SavedRevision(path, sha256(data).hexdigest(), len(data))
    path.unlink()
    monitor._enqueue(relative)
    check_pending(monitor, path)
    clock.now += 1.1
    check_pending(monitor, path)
    assert events == [FileRemoved(path)]


def test_validation_rejects_bytes_changed_during_hashing(tmp_path, monkeypatch):
    path = tmp_path / "changing.pptx"
    original = pptx_bytes("initial")
    path.write_bytes(original)
    original_digest = saved_files.file_digest

    def read_then_change(stream, algorithm):
        digest = original_digest(stream, algorithm)
        path.write_bytes(original + b"\n")
        return digest

    monkeypatch.setattr(saved_files, "file_digest", read_then_change)
    with pytest.raises(OSError, match="changed"):
        watching._saved_revision(path)


@pytest.mark.parametrize(
    "event", [SavedRevision(Path("deck.pptx"), "sha256", 42), FileRemoved(Path("deck.pptx"))]
)
def test_event_fields_cannot_be_reassigned_by_consumers(event):
    with pytest.raises(FrozenInstanceError):
        event.source_path = Path("other.pptx")


def test_event_contract_import_does_not_load_watchdog_com_qt_or_sqlite():
    script = """
import builtins
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'watchdog', 'win32com', 'pythoncom', 'PySide6', 'sqlite3'}:
        raise AssertionError('Concrete adapter dependency imported by event contracts')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from slideguard.application.ports import SavedRevision, FileRemoved, FileWatchEvent, FileWatchCallback
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_watch_cli_keeps_save_logs_and_restores_logging_state(tmp_path, monkeypatch):
    from slideguard import watch_cli

    path = tmp_path / "CLI 발표.pptx"
    data = pptx_bytes("CLI compatibility")
    log = tmp_path / "watch.log"
    instances = []
    before = watching.LOGGER.level, watching.LOGGER.propagate, list(watching.LOGGER.handlers)

    class WritingMonitor(SavedFileMonitor):
        def start(self):
            super().start()
            path.write_bytes(data)
            instances.append(self)

    def wait_for_save_then_interrupt(_):
        deadline = time.monotonic() + 5
        while "SAVE_DETECTED" not in log.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        raise KeyboardInterrupt

    monkeypatch.setattr(watch_cli, "SavedFileMonitor", WritingMonitor)
    monkeypatch.setattr(watch_cli, "time", SimpleNamespace(sleep=wait_for_save_then_interrupt))
    assert watch_cli.watch_directory(tmp_path, recursive=False, settle_seconds=0.1, log_file=log) == 0
    content = log.read_text(encoding="utf-8")
    assert "WATCH_STARTED" in content and "WATCH_STOPPED" in content
    assert "SAVE_DETECTED | 저장 감지" in content
    assert f"sha256={sha256(data).hexdigest()} | bytes={len(data)}" in content
    assert "CLI 발표.pptx" in content
    assert instances and not instances[0].is_running
    assert (watching.LOGGER.level, watching.LOGGER.propagate, watching.LOGGER.handlers) == before


def test_watch_extract_emits_snapshot_json_and_keeps_logs_separate(tmp_path, monkeypatch, capsys):
    import json

    from slideguard import bootstrap, watch_cli
    from slideguard.domain.models import Box, DeckSnapshot, Shape

    box = Box(5, 6, 7, 8)
    deck = DeckSnapshot("deck.pptx", "sha", 1, 960, 540, (Shape(1, 1, "text", box, visible_object_box=box),))
    stopped = []

    def collection(directory, callback, **options):
        assert directory == tmp_path and options["recursive"]
        return SimpleNamespace(
            start=lambda: callback(deck), stop=lambda: stopped.append(True), is_running=True
        )

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(bootstrap, "build_snapshot_collection", collection)
    monkeypatch.setattr(watch_cli, "time", SimpleNamespace(sleep=interrupt))
    assert (
        watch_cli.watch_directory(tmp_path, recursive=True, settle_seconds=0.1, log_file=None, extract=True)
        == 0
    )
    stdout, stderr = capsys.readouterr()
    assert json.loads(stdout)["shapes"][0]["visible_object_box"] == {"x": 5, "y": 6, "width": 7, "height": 8}
    assert "Ctrl+C" in stderr and stopped == [True]
