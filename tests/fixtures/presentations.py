import io
from dataclasses import dataclass
from hashlib import sha256

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt


@dataclass(frozen=True)
class FixtureDeck:
    data: bytes
    # (slide number, top-level shape id, name); group children are intentionally absent.
    shapes: tuple[tuple[int, int, str], ...]
    text: str

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


def _font(run, *, size, color, bold):
    run.font.name = "Malgun Gothic"
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(*color)
    run.font.bold = bold


def make_deck(label: str = "첫 저장") -> FixtureDeck:
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Pt(960), Pt(540)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    plain = slide.shapes.add_textbox(Pt(72), Pt(72), Pt(400), Pt(90))
    plain.name = "basic-text"
    plain.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    plain.text = f"기본 텍스트 {label}"
    _font(plain.text_frame.paragraphs[0].runs[0], size=24, color=(12, 34, 56), bold=False)

    mixed = slide.shapes.add_textbox(Pt(72), Pt(200), Pt(600), Pt(100))
    mixed.name = "mixed-text"
    mixed.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    paragraph = mixed.text_frame.paragraphs[0]
    small, large = paragraph.add_run(), paragraph.add_run()
    small.text, large.text = "작은 글자 ", "큰 글자"
    _font(small, size=18, color=(12, 34, 56), bold=False)
    _font(large, size=30, color=(170, 40, 80), bold=True)

    presentation.slides.add_slide(presentation.slide_layouts[6])  # Empty slide.
    unsupported = presentation.slides.add_slide(presentation.slide_layouts[6])
    table = unsupported.shapes.add_table(1, 2, Pt(40), Pt(40), Pt(400), Pt(80))
    table.name = "unsupported-table"
    table.table.cell(0, 0).text = "표 내부는 순회하지 않음"
    image = io.BytesIO()
    Image.new("RGB", (2, 2), (20, 80, 140)).save(image, format="PNG")
    image.seek(0)
    picture = unsupported.shapes.add_picture(image, Pt(500), Pt(40), Pt(80), Pt(80))
    picture.name = "unsupported-picture"
    group = unsupported.shapes.add_group_shape()
    group.name = "unsupported-group"
    group.shapes.add_textbox(Pt(40), Pt(200), Pt(400), Pt(80)).text = "그룹 내부는 순회하지 않음"

    shapes = tuple(
        (number, shape.shape_id, shape.name)
        for number, current in enumerate(presentation.slides, start=1)
        for shape in current.shapes
    )
    output = io.BytesIO()
    presentation.save(output)
    return FixtureDeck(output.getvalue(), shapes, plain.text)
