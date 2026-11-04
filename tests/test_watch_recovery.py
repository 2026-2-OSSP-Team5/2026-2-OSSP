import io
import logging
import queue
import struct
import time
import zipfile
from types import SimpleNamespace

import pytest

from slideguard.adapters.watching import monitor as watching
from slideguard.adapters.watching.revisions import saved_revision
from tests.test_save_monitor import pptx_bytes


def corrupted_bytes():
    data = bytearray(pptx_bytes("압축 손상"))
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        entry = package.getinfo("ppt/presentation.xml")
        assert entry.compress_type == zipfile.ZIP_DEFLATED
        name, extra = struct.unpack_from("<HH", data, entry.header_offset + 26)
        start = entry.header_offset + 30 + name + extra
    data[start] = 0xFF
    return bytes(data)


@pytest.mark.parametrize("existing", [True, False])
def test_corrupt_compression_does_not_stop_other_files_and_repaired_file_is_collected(
    tmp_path, caplog, existing
):
    caplog.set_level(logging.WARNING, logger="slideguard.watch")
    broken = tmp_path / "broken.pptx"
    if existing:
        broken.write_bytes(corrupted_bytes())
    output = queue.Queue()
    monitor = watching.SavedFileMonitor(tmp_path, output.put, settle_seconds=0.05, retry_seconds=0.3)
    monitor.start()
    try:
        if not existing:
            broken.write_bytes(corrupted_bytes())
        deadline = time.monotonic() + 5
        while not any("SAVE_UNREADABLE" in record.message for record in caplog.records):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert monitor.is_running and output.empty()
        good = tmp_path / "valid.pptx"
        good.write_bytes(pptx_bytes("정상 파일"))
        assert output.get(timeout=5) == good
        broken.write_bytes(pptx_bytes("복구 저장"))
        monitor.request_retry(broken)
        assert output.get(timeout=5) == broken
        assert monitor.is_running
    finally:
        monitor.stop()


def test_saved_revision_normalizes_decompression_failure(tmp_path):
    path = tmp_path / "compressed.pptx"
    path.write_bytes(corrupted_bytes())
    with pytest.raises(ValueError, match="Damaged PPTX compressed data"):
        saved_revision(path)


def test_failed_validation_uses_increasing_delay_without_blocking_the_worker(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(watching.time, "monotonic", lambda: clock.now)
    path = tmp_path / "pending.pptx"
    path.write_bytes(pptx_bytes("검증 대기"))
    calls = []

    def fail(_):
        calls.append(clock.now)
        raise ValueError("temporary damaged data")

    monkeypatch.setattr(watching, "_saved_revision", fail)
    monitor = watching.SavedFileMonitor(tmp_path, settle_seconds=0.1)
    monitor.request_retry(path)
    candidate = monitor._pending[path]
    for value in (101, 101.2, 101.5, 102, 102.5):
        clock.now = value
        monitor._check_pending(path, candidate)
    assert calls == [101, 101.5, 102.5]
    assert candidate.next_attempt == 104.5
    # An explicit retry creates a fresh schedule, without restarting the observer.
    monitor.request_retry(path)
    assert monitor._pending[path] is not candidate
