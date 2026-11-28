"""검사 속도: 슬라이드 PNG 내보내기(한 번 0.1~0.3초)를 꼭 필요한 만큼만 하는지."""

from types import SimpleNamespace

from PIL import Image

from slideguard.adapters.powerpoint import render as render_module
from slideguard.domain.models import Box, DeckSnapshot, ObjectLocator, Shape


def _shape(slide, shape_id, *, background=None, kind="shape", text="글자"):
    return Shape(
        slide,
        shape_id,
        f"s{shape_id}",
        Box(100, 100, 200, 50),
        text=text,
        background=background,
        rendered_box=Box(105, 105, 150, 30),
        supported=True,
        effective_visible=True,
        locator=ObjectLocator((shape_id,), kind, (1, 1) if kind == "table_cell" else ()),
    )


def test_only_slides_that_need_pixels_are_rendered_and_shapes_are_not_probed(monkeypatch, tmp_path):
    exports, probes = [], []

    def export(slide, _target, width, height):
        exports.append(slide.number)
        return Image.new("RGB", (width, height), "white")

    def probe(cells, *_args):
        probes.extend(cell.key for cell in cells)
        return {cell.key: Box(1, 2, 3, 4) for cell in cells}

    monkeypatch.setattr(render_module, "export_slide", export)
    monkeypatch.setattr(render_module, "measure_table_cells", probe)
    deck = DeckSnapshot(
        "d.pptx",
        "abc",
        3,
        960,
        540,
        shapes=(
            _shape(1, 2),  # 배경색 모름 → 슬라이드 1은 한 번 그림
            _shape(1, 3),
            _shape(2, 2, background=(255, 255, 255)),  # 배경색을 다 앎 → 슬라이드 2는 안 그림
            _shape(3, 4, kind="table_cell", background=(255, 255, 255)),  # 표 칸 → 그리고 픽셀 측정
        ),
    )
    slides = SimpleNamespace(Item=lambda number: SimpleNamespace(number=number))
    result = render_module.render_evidence(SimpleNamespace(Slides=slides), deck, tmp_path / "x.png")
    assert exports == [1, 3]  # 슬라이드마다 최대 한 번 (예전: 객체마다 3번)
    assert len(probes) == 1  # 표 칸만 픽셀 측정 (일반 글상자 3개는 측정하지 않음)
    by_id = {(s.slide_number, s.shape_id): s for s in result.shapes}
    assert by_id[(1, 2)].background_samples  # 대비 검사에 쓰는 배경 픽셀은 그대로 모음
    assert by_id[(3, 4)].rendered_box == Box(1, 2, 3, 4)  # 표 칸은 픽셀로 잰 글자 위치
