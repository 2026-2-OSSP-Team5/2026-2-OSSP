from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

from slideguard.adapters.powerpoint.patcher import PowerPointPatcher
from slideguard.adapters.powerpoint.restructure import prepare_split_proposals
from slideguard.bootstrap import build_analyzer, build_powerpoint_reader
from slideguard.domain.policy import AnalysisPolicy
from slideguard.ui.preview import render_slide_previews


def _deck(path: Path):
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = "분할 검증"
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "원문을 유지하는 본문 분할"
    slide.shapes.title.left, slide.shapes.title.top = Inches(0.7), Inches(0.5)
    slide.shapes.title.width, slide.shapes.title.height = Inches(11.8), Inches(0.65)
    for run in slide.shapes.title.text_frame.paragraphs[0].runs:
        run.font.size = Pt(28)
    body = slide.placeholders[1]
    body.left, body.top = Inches(0.7), Inches(1.4)
    body.width, body.height = Inches(11.8), Inches(5.4)
    frame = body.text_frame
    frame.clear()
    expected = []
    for index in range(24):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = f"{index + 1:02d}. 원문 보존 항목 {index + 1} — 기호와 한글을 그대로 유지합니다."
        paragraph.font.size = Pt(12)
        paragraph.level = 1 if index % 4 in (1, 2) else 0
        numbering = OxmlElement("a:buAutoNum")
        numbering.set("type", "arabicPeriod")
        numbering.set("startAt", str(index + 7))
        paragraph._p.get_or_add_pPr().insert(0, numbering)
        expected.append(paragraph.text)
    deck.save(path)
    return expected


@pytest.mark.powerpoint
def test_local_split_preserves_content_and_validates_all_pages(tmp_path, powerpoint_environment):
    source, output = tmp_path / "source.pptx", tmp_path / "result.pptx"
    expected = _deck(source)
    original = source.read_bytes()
    policy = AnalysisPolicy()
    report = build_analyzer(build_powerpoint_reader(render=True)).execute(source, policy)
    body = next(s for s in report.deck.shapes if s.slide_number == 2 and s.shape_id == 3)
    before_list = [
        (dict(p.properties).get("IndentLevel"), dict(p.properties).get("BulletNumber"))
        for p in body.spans
        if p.kind == "paragraph" and p.text.strip()
    ]
    assert any(number and number > 1 for _level, number in before_list), before_list
    prepare_split_proposals(report, policy)
    splits = [p for p in report.proposals if p.property == "slide.split"]
    assert len(splits) == 1, report.proposal_notes
    proposal = splits[0]
    assert proposal.after >= 2
    assert proposal.payload["validated"]
    patcher = PowerPointPatcher()
    patcher.apply_to_copy(report, [proposal.id], output, policy)
    assert source.read_bytes() == original
    result = Presentation(output)
    assert len(result.slides) == 1 + proposal.after
    actual = []
    for slide in list(result.slides)[1:]:
        shapes = sorted(
            (s for s in slide.shapes if s.has_text_frame and s != slide.shapes.title),
            key=lambda s: (s.top, s.left),
        )
        for shape in shapes:
            actual.extend(p.text for p in shape.text_frame.paragraphs if p.text)
    assert actual == expected
    for slide in list(result.slides)[1:]:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text.strip().startswith(
                tuple(f"{n:02d}." for n in range(1, 25))
            ):
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        if run.text.strip():
                            assert (run.font.size or paragraph.font.size).pt >= 18
    snapshot = build_powerpoint_reader(render=False).read(output)
    after_list = []
    for shape in sorted(snapshot.shapes, key=lambda s: (s.slide_number, s.box.y if s.box else 0, s.shape_id)):
        if (
            shape.text.strip().startswith(tuple(f"{n:02d}." for n in range(1, 25)))
            and shape.effective_visible is not False
        ):
            after_list.extend(
                (dict(p.properties).get("IndentLevel"), dict(p.properties).get("BulletNumber"))
                for p in shape.spans
                if p.kind == "paragraph" and p.text.strip()
            )
    assert after_list == before_list
    images = render_slide_previews(output, tuple(range(2, len(result.slides) + 1)))
    assert len(images) == proposal.after


@pytest.mark.powerpoint
def test_picture_and_caption_remain_on_one_page_without_media_duplication(tmp_path, powerpoint_environment):
    source, output = tmp_path / "picture.pptx", tmp_path / "picture-result.pptx"
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    deck.slides.add_slide(deck.slide_layouts[0]).shapes.title.text = "표지"
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "그림과 본문을 함께 유지"
    slide.shapes.title.left, slide.shapes.title.top = Inches(0.7), Inches(0.5)
    slide.shapes.title.width, slide.shapes.title.height = Inches(11.8), Inches(0.65)
    caption = slide.shapes.add_textbox(Inches(0.7), Inches(1.5), Inches(4), Inches(0.8))
    caption.text = "그림 1. 원본 그림에 대한 설명입니다."
    buffer = BytesIO()
    Image.new("RGB", (160, 100), "#357CA8").save(buffer, format="PNG")
    media = buffer.getvalue()
    slide.shapes.add_picture(BytesIO(media), Inches(7), Inches(1.5), width=Inches(2))
    body = slide.shapes.add_textbox(Inches(0.7), Inches(3), Inches(11.8), Inches(4))
    expected = []
    for index in range(24):
        p = body.text_frame.paragraphs[0] if index == 0 else body.text_frame.add_paragraph()
        p.text = f"후속 문단 {index + 1}. 순서를 유지해야 하는 본문입니다."
        p.font.size = Pt(12)
        expected.append(p.text)
    deck.save(source)
    original = source.read_bytes()
    report = build_analyzer(build_powerpoint_reader(render=True)).execute(source, AnalysisPolicy())
    prepare_split_proposals(report, AnalysisPolicy())
    proposal = next((p for p in report.proposals if p.property == "slide.split"), None)
    assert proposal is not None, report.proposal_notes
    PowerPointPatcher().apply_to_copy(report, [proposal.id], output, AnalysisPolicy())
    result = Presentation(output)
    pictures = [
        (i, shape) for i, s in enumerate(result.slides) for shape in s.shapes if shape.shape_type == 13
    ]
    assert len(pictures) == 1 and pictures[0][1].image.blob == media
    assert any(
        shape.has_text_frame and "그림 1." in shape.text for shape in result.slides[pictures[0][0]].shapes
    )
    actual = [
        p.text
        for s in result.slides
        for shape in sorted(s.shapes, key=lambda x: (x.top, x.left))
        if shape.has_text_frame
        for p in shape.text_frame.paragraphs
        if p.text.startswith("후속 문단")
    ]
    assert actual == expected
    assert source.read_bytes() == original
    for slide in result.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text.startswith("후속 문단"):
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        if run.text.strip():
                            assert (run.font.size or paragraph.font.size).pt >= 18
