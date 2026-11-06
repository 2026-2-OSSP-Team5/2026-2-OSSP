"""실제 PowerPoint로 '배경을 지정하지 않은 슬라이드'의 배경색 판정을 확인합니다. (Windows, --run-powerpoint)"""

import sys

import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from slideguard.bootstrap import build_powerpoint_reader
from slideguard.checks.contrast import ContrastRule
from slideguard.domain.policy import AnalysisPolicy


def test_unfilled_text_on_default_theme_slide_gets_white_background(request, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    pytest.importorskip("win32com.client", reason="pywin32 is not installed")

    # python-pptx 기본 템플릿: 흰 테마 배경, 슬라이드·레이아웃 모두 마스터 배경을 따름
    path = tmp_path / "기본 배경.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])  # 빈 화면
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))  # 채우기 없음
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "Light gray text"
    run.font.size = Pt(24)
    run.font.color.rgb = RGBColor(0xC8, 0xC8, 0xC8)
    presentation.save(path)

    deck = build_powerpoint_reader(render=False).read(path)
    shape = next(item for item in deck.shapes if item.text.strip() == "Light gray text")

    assert shape.background == (255, 255, 255), shape.notes
    findings = ContrastRule().evaluate(deck, AnalysisPolicy())
    assert [item.severity for item in findings if item.shape_key == shape.key] == ["warning"]
