"""실제 PowerPoint로 '원본에 적용 (백업 후 저장)'을 확인합니다. (Windows, --run-powerpoint)"""

import gc
import sys
from hashlib import sha256

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from slideguard.adapters.powerpoint.patcher import PowerPointPatcher
from slideguard.bootstrap import build_analyzer, build_powerpoint_reader
from slideguard.domain.models import AnalysisReport
from slideguard.domain.policy import AnalysisPolicy


def _require_powerpoint(request):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    pytest.importorskip("win32com.client", reason="pywin32 is not installed")


def _small_text_deck(path):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    box.text_frame.paragraphs[0].add_run().text = "SlideGuard small text"
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(10)
    presentation.save(path)
    return path.read_bytes()


def _report_with_size_proposal(path):
    deck = build_powerpoint_reader(render=False).read(path)
    analyzer = build_analyzer()
    policy = AnalysisPolicy()
    report = AnalysisReport(deck=deck)
    for rule in analyzer.rules:
        report.findings.extend(rule.evaluate(deck, policy))
    report.status = "analyzed"
    report.proposals = analyzer.planner.propose(report, policy)
    selected = next(item for item in report.proposals if item.property == "font.size" and item.before == 10)
    return report, policy, selected


def _small_text_size(path):
    snapshot = build_powerpoint_reader(render=False).read(path)
    return min(
        run.font_size_pt
        for shape in snapshot.shapes
        if shape.text.strip() == "SlideGuard small text"
        for run in shape.runs
        if run.text.strip()
    )


def test_closed_original_is_backed_up_then_saved(request, tmp_path):
    _require_powerpoint(request)
    source = tmp_path / "원본.pptx"
    original_bytes = _small_text_deck(source)
    report, policy, selected = _report_with_size_proposal(source)

    patcher = PowerPointPatcher()
    backup = patcher.apply_to_original(report, [selected.id], tmp_path / "backups", policy)

    assert backup.read_bytes() == original_bytes
    assert sha256(source.read_bytes()).hexdigest() != report.deck.source_sha256
    assert patcher.last_result.applied and not patcher.last_result.was_open
    assert _small_text_size(source) == 14
    assert _small_text_size(backup) == 10


def test_original_open_in_powerpoint_is_patched_in_place_and_stays_open(request, tmp_path):
    _require_powerpoint(request)
    import pythoncom
    import win32com.client

    source = tmp_path / "열린 원본.pptx"
    original_bytes = _small_text_deck(source)
    report, policy, selected = _report_with_size_proposal(source)

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    application = presentation = None
    try:
        application = win32com.client.DispatchEx("PowerPoint.Application")
        presentation = application.Presentations.Open(str(source), 0, 0, -1)  # 사용자가 연 것처럼 창과 함께

        patcher = PowerPointPatcher()
        backup = patcher.apply_to_original(report, [selected.id], tmp_path / "backups", policy)

        assert patcher.last_result.was_open
        assert backup.read_bytes() == original_bytes
        assert presentation.Saved == -1  # 우리가 저장했고, 문서는 열린 채로 남음
        size = presentation.Slides(1).Shapes(1).TextFrame2.TextRange.Font.Size
        assert size == 14
    finally:
        if presentation is not None:
            presentation.Close()
        presentation = application = None
        gc.collect()
        pythoncom.CoUninitialize()

    assert _small_text_size(source) == 14


def test_unsaved_open_original_is_refused_and_backup_removed(request, tmp_path):
    _require_powerpoint(request)
    import pythoncom
    import win32com.client

    from slideguard.adapters.powerpoint.patcher import ApplyRejected

    source = tmp_path / "저장 안 한 원본.pptx"
    original_bytes = _small_text_deck(source)
    report, policy, selected = _report_with_size_proposal(source)

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    application = presentation = None
    try:
        application = win32com.client.DispatchEx("PowerPoint.Application")
        presentation = application.Presentations.Open(str(source), 0, 0, -1)
        presentation.Slides(1).Shapes(1).Left = presentation.Slides(1).Shapes(1).Left + 1  # 저장 안 한 편집

        with pytest.raises(ApplyRejected, match="저장하지 않은"):
            PowerPointPatcher().apply_to_original(report, [selected.id], tmp_path / "backups", policy)
    finally:
        if presentation is not None:
            presentation.Saved = -1  # 테스트 편집은 버림
            presentation.Close()
        presentation = application = None
        gc.collect()
        pythoncom.CoUninitialize()

    assert source.read_bytes() == original_bytes
    assert not any((tmp_path / "backups").glob("*.pptx"))
