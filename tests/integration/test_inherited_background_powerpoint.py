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


def test_text_over_picture_is_judged_from_rendered_pixels(request, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    pytest.importorskip("win32com.client", reason="pywin32 is not installed")
    from PIL import Image

    # 왼쪽은 흰색, 오른쪽은 검정인 그림 위에 흰 글씨 → 배경색을 하나로 정할 수 없음
    picture = tmp_path / "half.png"
    image = Image.new("RGB", (400, 100), (255, 255, 255))
    for x in range(200, 400):
        for y in range(100):
            image.putpixel((x, y), (0, 0, 0))
    image.save(picture)
    path = tmp_path / "사진 위 글씨.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(str(picture), Inches(1), Inches(1), Inches(6), Inches(1.5))
    box = slide.shapes.add_textbox(Inches(1.5), Inches(1.3), Inches(5), Inches(0.8))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "White text over picture"
    run.font.size = Pt(24)
    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    presentation.save(path)

    deck = build_powerpoint_reader(render=True).read(path)
    shape = next(item for item in deck.shapes if item.text.strip() == "White text over picture")
    findings = [
        item for item in ContrastRule().evaluate(deck, AnalysisPolicy()) if item.shape_key == shape.key
    ]

    assert shape.background is None and len(shape.background_samples) >= 4
    assert [item.severity for item in findings] == ["review"]  # 흰 배경 부분에서 명암비 부족


def test_overflowing_text_touching_a_transparent_text_box_keeps_white_background(request, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Desktop PowerPoint COM integration requires Windows")
    if not request.config.getoption("--run-powerpoint"):
        pytest.skip("Desktop PowerPoint integration is opt-in: pass --run-powerpoint")
    pytest.importorskip("pythoncom", reason="pywin32 is not installed")
    pytest.importorskip("win32com.client", reason="pywin32 is not installed")
    from pptx.enum.text import MSO_AUTO_SIZE

    # 샘플 1번 슬라이드 상황: 28pt 글씨가 상자 아래로 넘쳐 다음 글상자(투명)에 닿음
    path = tmp_path / "넘친 글씨.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    upper = slide.shapes.add_textbox(Pt(40), Pt(160), Pt(420), Pt(50))
    upper.text_frame.word_wrap = True
    upper.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    upper_run = upper.text_frame.paragraphs[0].add_run()
    upper_run.text = "Overflowing light yellow text that wraps onto a second line"
    upper_run.font.size = Pt(28)
    upper_run.font.color.rgb = RGBColor(0xFF, 0xE6, 0x78)
    lower = slide.shapes.add_textbox(Pt(40), Pt(215), Pt(300), Pt(60))
    lower.text_frame.paragraphs[0].add_run().text = "Next box"
    presentation.save(path)

    deck = build_powerpoint_reader(render=False).read(path)
    shape = next(item for item in deck.shapes if item.text.startswith("Overflowing"))
    assert shape.background == (255, 255, 255), shape.notes
