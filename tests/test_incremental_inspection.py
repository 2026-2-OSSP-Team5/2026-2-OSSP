import json
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from pptx import Presentation

from slideguard.adapters.powerpoint.incremental import slide_fingerprints
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.storage.sqlite import _report_from_json
from slideguard.application.collect import SnapshotCollector
from slideguard.application.ports import ReadInterrupted
from slideguard.domain.models import AnalysisReport
from tests.test_powerpoint_reader import Collection, ShapeProxy
from tests.test_snapshot_collection import Source, revision, snapshot


def test_regular_inspection_never_prepares_split_proposals(tmp_path, monkeypatch):
    from slideguard.adapters.powerpoint import restructure
    from slideguard.bootstrap import build_analyzer
    from slideguard.domain.policy import AnalysisPolicy
    from slideguard.ui import overlay

    def forbidden(*_args, **_kwargs):
        pytest.fail("Regular inspection must not create split candidates")

    monkeypatch.setattr(restructure, "prepare_split_proposals", forbidden)
    monkeypatch.setattr(overlay, "build_analyzer", build_analyzer)
    monkeypatch.setattr(overlay, "build_analysis_policy", lambda _path: AnalysisPolicy())
    saved = []
    monkeypatch.setattr(overlay, "build_report_repository", lambda: SimpleNamespace(save=saved.append))
    outputs = []
    controller = SimpleNamespace(completed=SimpleNamespace(emit=outputs.append))
    overlay.OverlayController._process_snapshot(controller, snapshot(revision(tmp_path / "deck.pptx")))
    assert len(saved) == len(outputs) == 1


def test_split_request_only_attempts_selected_slide_and_preserves_other_notes(tmp_path, monkeypatch):
    from slideguard.adapters.powerpoint import restructure
    from slideguard.domain.models import Finding
    from slideguard.domain.policy import AnalysisPolicy
    from slideguard.planning.splitting import SplitUnavailable

    report = AnalysisReport(snapshot(revision(tmp_path / "deck.pptx")))
    report.findings = [
        Finding(f"typography:{n}:2", "typography", f"{n}:2", "warning", "small", 10, 20, "test")
        for n in (1, 2, 3)
    ]
    report.proposal_notes[report.findings[0].id] = "Earlier slide note"
    attempted = []

    def draft(_deck, _findings, _policy, slide):
        attempted.append(slide)
        raise SplitUnavailable("Not enough body paragraphs")

    monkeypatch.setattr(restructure, "build_draft", draft)
    restructure.prepare_split_proposals(report, AnalysisPolicy(), slide_numbers=(2,))
    assert attempted == [2]
    assert report.proposal_notes[report.findings[0].id] == "Earlier slide note"
    assert report.proposal_notes[report.findings[1].id] == "Not enough body paragraphs"
    assert report.findings[2].id not in report.proposal_notes


def test_toolbar_progress_drag_and_fixes_are_explicit(tmp_path):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication

    from slideguard.application.ports import AnalysisProgress
    from slideguard.ui.overlay import PANEL_HEIGHT, PANEL_SIDE_PADDING, OverlayWidget, PowerPointWindow
    from tests.test_overlay import _proposal_report

    app = QApplication.instance() or QApplication([])
    widget = OverlayWidget()
    calls = []
    widget.fixes_requested.connect(calls.append)
    try:
        widget.set_target(PowerPointWindow(0, QRect(0, 0, 1440, 900), "deck.pptx"))
        widget.set_collapsed(False)
        widget.show_report(_proposal_report(tmp_path / "deck.pptx", "hash"))
        app.processEvents()
        assert calls == []
        widget.view_fixes_button.click()
        assert calls == [widget._finding]
        assert widget.height() <= PANEL_HEIGHT
        widget._move_panel(QPoint(50, 240))
        assert widget.pos() == QPoint(50, 240)
        widget._move_panel(QPoint(10000, -10000))
        assert widget.geometry().right() <= 1439 - PANEL_SIDE_PADDING
        assert widget.geometry().top() >= 32
        widget.show_progress(AnalysisProgress(tmp_path / "deck.pptx", "read:2", 2, 10, 1))
        assert widget.progress_bar.maximum() == 10 and widget.progress_bar.value() == 2
        assert "2/10" in widget.status_label.text()
    finally:
        widget.close()


def deck_file(path, count=3):
    deck = Presentation()
    for number in range(count):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, 2000000, 1000000).text = f"Body {number}"
    deck.save(path)
    return path


class Session:
    def __init__(self):
        self.opens = 0

    def read_copy(self, path, extract, **_options):
        self.opens += 1
        deck = Presentation(path)
        proxy = SimpleNamespace(
            PageSetup=SimpleNamespace(SlideWidth=960.0, SlideHeight=540.0),
            Slides=Collection(
                [
                    SimpleNamespace(
                        Shapes=Collection(
                            [ShapeProxy(shape_id=s.shape_id, text=s.text) for s in slide.shapes]
                        )
                    )
                    for slide in deck.slides
                ]
            ),
        )
        return extract(proxy)


def test_only_edited_slide_is_remeasured_and_unchanged_deck_needs_no_com(tmp_path):
    path = deck_file(tmp_path / "deck.pptx")
    session = Session()
    reader = PowerPointDeckReader(session)
    first = reader.read_incremental(path)
    events = []
    same = reader.read_incremental(path, on_progress=events.append)
    assert session.opens == 1
    assert same == first
    assert events[-1].reused == 3

    deck = Presentation(path)
    deck.slides[1].shapes[0].text = "Edited body"
    deck.save(path)
    events.clear()
    changed = reader.read_incremental(path, on_progress=events.append)
    assert session.opens == 2
    assert [p.stage for p in events if p.stage.startswith("read:")] == ["read:2"]
    assert events[-1].reused == 2
    assert [s.text for s in changed.shapes] == ["Body 0", "Edited body", "Body 2"]
    assert changed.source_sha256 != first.source_sha256


def test_saved_measurements_survive_restart_and_slide_reordering(tmp_path):
    path = deck_file(tmp_path / "deck.pptx")
    first = PowerPointDeckReader(Session()).read_incremental(path)
    restored = _report_from_json(json.dumps(asdict(AnalysisReport(first)))).deck
    deck = Presentation(path)
    slides = deck.slides._sldIdLst
    last = slides[-1]
    slides.remove(last)
    slides.insert(0, last)
    deck.save(path)
    session = Session()
    reader = PowerPointDeckReader(session, baseline_provider=lambda _p: restored)
    result = reader.read_incremental(path)
    assert session.opens == 0
    assert [s.text for s in result.shapes] == ["Body 2", "Body 0", "Body 1"]
    assert [s.slide_number for s in result.shapes] == [1, 2, 3]


def test_theme_change_invalidates_dependent_slides(tmp_path):
    path = deck_file(tmp_path / "deck.pptx")
    before = slide_fingerprints(path)
    with ZipFile(path) as archive:
        parts = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    theme = next(name for name in parts if name.startswith("ppt/theme/") and name.endswith(".xml"))
    parts[theme] = parts[theme].replace(b"FFFFFF", b"FFFFFE")
    with ZipFile(path, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    after = slide_fingerprints(path)
    assert all(a[1] != b[1] for a, b in zip(before, after, strict=True))


def test_editor_view_changes_do_not_invalidate_slide_measurements(tmp_path):
    path = deck_file(tmp_path / "deck.pptx")
    before = slide_fingerprints(path)
    with ZipFile(path) as archive:
        parts = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    parts["ppt/viewProps.xml"] = parts["ppt/viewProps.xml"].replace(b"sldThumbnailView", b"sldView")
    with ZipFile(path, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    assert slide_fingerprints(path) == before


def test_yielded_reader_resumes_using_completed_slide(tmp_path):
    path = deck_file(tmp_path / "deck.pptx")
    session = Session()
    reader = PowerPointDeckReader(session)
    yield_now = False

    def progress(value):
        nonlocal yield_now
        if value.stage == "read" and value.completed == 1:
            yield_now = True

    with pytest.raises(ReadInterrupted):
        reader.read_incremental(path, should_yield=lambda: yield_now, on_progress=progress)
    events = []
    result = reader.read_incremental(path, on_progress=events.append)
    assert len(result.shapes) == 3
    assert events[-1].reused == 1
    assert [p.stage for p in events if p.stage.startswith("read:")] == ["read:2", "read:3"]


def test_manual_request_preempts_background_at_checkpoint_and_resumes(tmp_path):
    background = revision(tmp_path / "background.pptx")
    manual = revision(tmp_path / "manual.pptx")
    entered, release = threading.Event(), threading.Event()
    output, failures = [], []
    first = True

    class Reader:
        def read_incremental(self, path, *, should_yield, on_progress):
            nonlocal first
            if path == background.source_path and first:
                first = False
                entered.set()
                assert release.wait(5)
                if should_yield():
                    raise ReadInterrupted()
            return snapshot(background if path == background.source_path else manual)

    collector = SnapshotCollector(
        Reader(), Source(background, manual), output.append, on_failure=failures.append
    )
    try:
        collector.start()
        assert entered.wait(5)
        collector.request(manual.source_path)
        release.set()
        assert collector.wait_idle(5)
        assert [Path(d.source_path).name for d in output] == ["manual.pptx", "background.pptx"]
        assert failures == []
    finally:
        release.set()
        collector.stop()


def test_active_presentation_precedes_background_startup_scan(tmp_path):
    background = revision(tmp_path / "background.pptx")
    active = revision(tmp_path / "active.pptx")
    output = []

    class Reader:
        def read(self, path):
            return snapshot(active if path == active.source_path else background)

    collector = SnapshotCollector(Reader(), Source(background, active), output.append)
    try:
        collector.prioritize(active.source_path)
        collector.start()
        assert collector.wait_idle(5)
        assert [d.source_path for d in output] == [str(active.source_path), str(background.source_path)]
    finally:
        collector.stop()
