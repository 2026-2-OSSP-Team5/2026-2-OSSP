import json
from time import perf_counter

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from slideguard.adapters.powerpoint.incremental import slide_fingerprints
from slideguard.adapters.powerpoint.patcher import ComDocumentSession
from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.powerpoint.session import PowerPointReadSession


@pytest.mark.powerpoint
def test_powerpoint_save_only_invalidates_edited_slide(tmp_path, powerpoint_environment):
    path = tmp_path / "office-save.pptx"
    deck = Presentation()
    for number in range(3):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2)).text = f"Slide {number}"
    deck.save(path)
    # Let Office normalize a generated document before comparing actual Office saves.
    with ComDocumentSession(str(path)) as document:
        document._pres.Saved = 0
        document.save()
    before = slide_fingerprints(path)
    (tmp_path / "before.pptx").write_bytes(path.read_bytes())
    with ComDocumentSession(str(path)) as document:
        document._pres.Slides.Item(2).Shapes.Item(1).TextFrame.TextRange.Text = "Changed in PowerPoint"
        document.save()
    after = slide_fingerprints(path)
    assert [number for number, (a, b) in enumerate(zip(before, after, strict=True), 1) if a != b] == [2]


@pytest.mark.powerpoint
def test_changed_slide_only_with_real_powerpoint(tmp_path, powerpoint_environment):
    path = tmp_path / "incremental.pptx"
    deck = Presentation()
    for number in range(3):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        text = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(2))
        text.text = f"Incremental slide {number + 1}"
        text.text_frame.paragraphs[0].runs[0].font.size = Pt(24)
    deck.save(path)
    original = path.read_bytes()
    reader = PowerPointDeckReader(PowerPointReadSession(), render=True)
    started = perf_counter()
    first = reader.read_incremental(path)
    first_time = perf_counter() - started
    assert path.read_bytes() == original
    events = []
    started = perf_counter()
    same = reader.read_incremental(path, on_progress=events.append)
    same_time = perf_counter() - started
    assert same == first and events[-1].reused == 3
    assert not any(p.stage.startswith("read:") for p in events)
    deck = Presentation(path)
    deck.slides[1].shapes[0].text_frame.paragraphs[0].runs[0].text = "Changed second slide"
    deck.save(path)
    events.clear()
    started = perf_counter()
    changed = reader.read_incremental(path, on_progress=events.append)
    changed_time = perf_counter() - started
    assert [p.stage for p in events if p.stage.startswith("read:")] == ["read:2"]
    assert events[-1].reused == 2
    assert [s.text for s in changed.shapes] == [
        "Incremental slide 1",
        "Changed second slide",
        "Incremental slide 3",
    ]
    print(
        f"INCREMENTAL_BENCHMARK initial={first_time:.3f}s unchanged={same_time:.3f}s one_changed={changed_time:.3f}s"
    )
    (tmp_path / "timing.json").write_text(
        json.dumps(
            {
                "slides": 3,
                "initial_seconds": first_time,
                "unchanged_seconds": same_time,
                "one_changed_seconds": changed_time,
            }
        ),
        encoding="utf-8",
    )
