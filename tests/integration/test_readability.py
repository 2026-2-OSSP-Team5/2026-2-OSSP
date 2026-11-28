import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from slideguard.adapters.powerpoint.patcher import ComDocumentSession
from slideguard.bootstrap import build_analyzer, build_powerpoint_reader
from slideguard.checks.readability import ReadabilityRule
from slideguard.domain.policy import AnalysisPolicy


@pytest.mark.powerpoint
def test_single_line_paragraphs_with_blank_separator_do_not_trigger_spacing(tmp_path, powerpoint_environment):
    source = tmp_path / "one-line-paragraphs.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    body = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(8), Inches(3))
    body.text = "책의 상태를 미리 알고 예약\n\n도서관 이용 만족도 상승"
    for paragraph in body.text_frame.paragraphs:
        paragraph.font.size = Pt(32)
        paragraph.line_spacing = 1.0
    deck.save(source)
    original = source.read_bytes()
    snapshot = build_powerpoint_reader().read(source)
    measured = next(s for s in snapshot.shapes if s.text.strip())
    assert all(dict(s.properties).get("Length", 0) > 0 for s in measured.spans if s.kind == "line")
    assert not any("spacing" in f.rule_id for f in ReadabilityRule().evaluate(snapshot, AnalysisPolicy()))
    assert source.read_bytes() == original


@pytest.mark.powerpoint
def test_real_paragraph_formatting_reaches_readability_checks(tmp_path, powerpoint_environment):
    source = tmp_path / "paragraphs.pptx"
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[0]).shapes.title.text = "검사 표지"
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    title = slide.shapes.add_textbox(Inches(0.7), Inches(0.5), Inches(8), Inches(0.6))
    title.text = "문단 서식 검사"
    body = slide.shapes.add_textbox(Inches(0.7), Inches(1.5), Inches(5), Inches(4.5))
    body.text_frame.word_wrap = True
    paragraph = body.text_frame.paragraphs[0]
    paragraph.text = (
        "이 문단은 여러 줄의 본문으로 줄 간격과 가운데 정렬 및 강조 서식이 실제 검사에 전달되는지 확인합니다. "
        * 3
    )
    paragraph.alignment = PP_ALIGN.CENTER
    paragraph.line_spacing = 1.0
    paragraph.font.size = Pt(18)
    paragraph.font.italic = True
    paragraph.font.underline = True
    deck.save(source)
    original = source.read_bytes()
    report = build_analyzer(build_powerpoint_reader(render=True)).execute(source, AnalysisPolicy())
    measured = next(s for s in report.deck.shapes if s.slide_number == 2 and s.shape_id == body.shape_id)
    assert sum(s.kind == "line" and bool(s.text.strip()) for s in measured.spans) >= 2
    rules = {f.rule_id for f in report.findings}
    assert any(rule.startswith("readability.spacing.") for rule in rules)
    assert any(rule.startswith("readability.alignment.") for rule in rules)
    assert "readability.italic" in rules
    assert "readability.underline" in rules
    assert source.read_bytes() == original


@pytest.mark.powerpoint
def test_actual_chart_colors_and_plot_background_are_measured(tmp_path, powerpoint_environment):
    source = tmp_path / "chart.pptx"
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[0]).shapes.title.text = "표지"
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "결과"
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("계열 1", [1, 2])
    data.add_series("계열 2", [2, 3])
    shape = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.8), Inches(8), Inches(4.5), data
    )
    shape_id = shape.shape_id
    deck.save(source)
    with ComDocumentSession(str(source)) as document:
        shapes = document._pres.Slides.Item(2).Shapes
        chart = next(
            shapes.Item(i).Chart for i in range(1, shapes.Count + 1) if shapes.Item(i).Id == shape_id
        )
        plot = chart.PlotArea.Format.Fill
        plot.Visible = -1
        plot.Solid()
        plot.ForeColor.RGB = 0xFFFFFF
        plot.Transparency = 0
        for index, color in enumerate((0xBBBBBB, 0xCCCCCC), 1):
            series = chart.SeriesCollection(index)
            series.Format.Fill.Visible = -1
            series.Format.Fill.Solid()
            series.Format.Fill.ForeColor.RGB = color
            series.Format.Fill.Transparency = 0
            series.HasDataLabels = False
        series = plot = chart = shapes = None
        document.save()
    report = build_analyzer(build_powerpoint_reader(render=True)).execute(source, AnalysisPolicy())
    rules = {f.rule_id for f in report.findings}
    assert "readability.chart_contrast" in rules
    assert "readability.color_only" in rules
    assert "readability.message" in rules
