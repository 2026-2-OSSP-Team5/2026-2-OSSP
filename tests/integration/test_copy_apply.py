import sys
from hashlib import sha256

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from slideguard.bootstrap import build_analyzer, build_powerpoint_reader, build_presentation_patcher
from slideguard.domain.models import AnalysisReport
from slideguard.domain.policy import AnalysisPolicy
from slideguard.ui.preview import render_slide_previews


def _text_sizes(snapshot):
    return {
        shape.text.strip(): min(
            run.font_size_pt for run in shape.runs if run.text.strip() and run.font_size_pt is not None
        )
        for shape in snapshot.shapes
        if shape.text.strip() and any(run.text.strip() and run.font_size_pt is not None for run in shape.runs)
    }


def test_real_powerpoint_applies_selected_proposal_to_one_copy_and_renders_result(request, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    pytest.importorskip("win32com.client", reason="pywin32 is not installed")
    source = tmp_path / "source.pptx"
    output = tmp_path / "source_SlideGuard.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    text_box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    text_box.text_frame.paragraphs[0].add_run().text = "SlideGuard small text"
    text_box.text_frame.paragraphs[0].runs[0].font.size = Pt(10)
    unchecked_box = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(5), Inches(1))
    unchecked_box.text_frame.paragraphs[0].add_run().text = "SlideGuard unchecked text"
    unchecked_box.text_frame.paragraphs[0].runs[0].font.size = Pt(11)
    presentation.save(source)
    original_bytes = source.read_bytes()

    deck = build_powerpoint_reader(render=False).read(source)
    analyzer = build_analyzer()
    policy = AnalysisPolicy()
    report = AnalysisReport(deck=deck)
    for rule in analyzer.rules:
        report.findings.extend(rule.evaluate(deck, policy))
    report.executed_rules = tuple(rule.id for rule in analyzer.rules)
    report.status = "analyzed"
    report.proposals = analyzer.planner.propose(report, policy)
    selected = next(item for item in report.proposals if item.property == "font.size" and item.before == 10)
    assert len([item for item in report.proposals if item.property == "font.size"]) == 2

    patcher = build_presentation_patcher()
    patcher.apply_to_copy(report, [selected.id], output, policy)

    assert output.is_file()
    assert source.read_bytes() == original_bytes
    assert sha256(source.read_bytes()).hexdigest() == report.deck.source_sha256
    assert patcher.last_result is not None
    assert len(patcher.last_result.applied) == 1
    assert patcher.last_result.rejected == []

    original_snapshot = build_powerpoint_reader(render=False).read(source)
    copy_snapshot = build_powerpoint_reader(render=False).read(output)
    assert _text_sizes(original_snapshot) == {
        "SlideGuard small text": 10,
        "SlideGuard unchecked text": 11,
    }
    assert _text_sizes(copy_snapshot) == {
        "SlideGuard small text": 14,
        "SlideGuard unchecked text": 11,
    }

    original_preview = render_slide_previews(source, (1,))
    copy_preview = render_slide_previews(output, (1,))
    assert original_preview[1].startswith(b"\x89PNG")
    assert copy_preview[1].startswith(b"\x89PNG")
