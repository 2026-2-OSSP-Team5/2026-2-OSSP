from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from slideguard.adapters.powerpoint.pixel_bounds import measure_pixels
from slideguard.domain.models import Box, ObjectLocator, Shape
from tests.test_powerpoint_text import Collection


@pytest.mark.parametrize("reflow", [False, True])
def test_cell_pixel_probe_uses_slide_pixels_and_rejects_table_reflow(reflow):
    row = SimpleNamespace(Height=40)

    class CellText:
        def __init__(self):
            self._text = "Cell"

        @property
        def Text(self):
            return self._text

        @Text.setter
        def Text(self, value):
            self._text = value
            if reflow:
                probe.Table.Rows.Item(1).Height = 35

    cell = SimpleNamespace(
        Left=100, Top=200, Width=140, Height=40, TextFrame=SimpleNamespace(AutoSize=0, TextRange=CellText())
    )
    table = SimpleNamespace(
        Id=7,
        Left=100,
        Top=200,
        Width=140,
        Height=40,
        Table=SimpleNamespace(
            Rows=Collection([row]),
            Columns=Collection([SimpleNamespace(Width=140)]),
            Cell=lambda *_args: SimpleNamespace(Shape=cell),
        ),
    )
    probe = deepcopy(table)
    # Cell lookup must resolve the probe's private text frame.
    probe_cell = deepcopy(cell)
    probe.Table.Cell = lambda *_args: SimpleNamespace(Shape=probe_cell)
    deleted = []
    duplicate = SimpleNamespace(Shapes=Collection([probe]), Delete=lambda: deleted.append(True))
    slide = SimpleNamespace(Shapes=Collection([table]), Duplicate=lambda: Collection([duplicate]))

    def render(_slide):
        image = Image.new("RGB", (400, 400), "white")
        if probe_cell.TextFrame.TextRange.Text:
            ImageDraw.Draw(image).rectangle((110, 210, 149, 224), fill="black")
        return image

    source = Shape(
        1,
        7,
        "cell",
        Box(100, 200, 140, 40),
        text="Cell",
        effective_visible=True,
        locator=ObjectLocator((7,), "table_cell", (1, 1)),
    )
    measured = measure_pixels(source, slide, render(slide), render, 1, 1)

    assert measured.rendered_box == (None if reflow else Box(110, 210, 40, 15))
    assert measured.visible_text_box == measured.rendered_box
    assert deleted == [True]
    assert cell.TextFrame.TextRange.Text == "Cell"


def test_table_cells_share_one_probe_slide_and_one_export_each():
    """칸 3개 → 슬라이드 복제 1번, 그리기 1+3번 (예전: 복제 3번, 그리기 6번)."""
    from slideguard.adapters.powerpoint.pixel_bounds import measure_table_cells

    texts = {(1, 1): "A", (1, 2): "B", (2, 1): "C"}
    places = {(1, 1): (10, 10, 30, 20), (1, 2): (60, 10, 80, 20), (2, 1): (10, 60, 30, 70)}

    class Text:
        def __init__(self, cell):
            self.cell = cell

        @property
        def Text(self):
            return texts[self.cell]

        @Text.setter
        def Text(self, value):
            texts[self.cell] = value

    def cell_shape(row, column):
        return SimpleNamespace(
            Shape=SimpleNamespace(TextFrame=SimpleNamespace(AutoSize=0, TextRange=Text((row, column))))
        )

    table = SimpleNamespace(
        Rows=Collection([SimpleNamespace(Height=40)] * 2),
        Columns=Collection([SimpleNamespace(Width=50)] * 2),
        Cell=cell_shape,
    )
    container = SimpleNamespace(Id=7, Left=0, Top=0, Width=100, Height=80, Table=table)
    duplicates, renders = [], []
    duplicate = SimpleNamespace(Shapes=Collection([container]), Delete=lambda: None)

    def duplicate_slide():
        duplicates.append(1)
        return Collection([duplicate])

    slide = SimpleNamespace(Shapes=Collection([container]), Duplicate=duplicate_slide)

    def render(_slide):
        renders.append(1)
        image = Image.new("RGB", (100, 80), "white")
        for key, text in texts.items():
            if text:
                ImageDraw.Draw(image).rectangle(places[key], fill="black")
        return image

    def cell(row, column):
        return Shape(
            1,
            7,
            "cell",
            Box(0, 0, 50, 40),
            text="x",
            effective_visible=True,
            locator=ObjectLocator((7,), "table_cell", (row, column)),
        )

    cells = [cell(1, 1), cell(1, 2), cell(2, 1)]
    boxes = measure_table_cells(cells, slide, render, 1, 1)
    assert len(duplicates) == 1 and len(renders) == 4
    assert [boxes[c.key] for c in cells] == [Box(10, 10, 21, 11), Box(60, 10, 21, 11), Box(10, 60, 21, 11)]


def test_cell_pixels_far_outside_the_cell_are_discarded():
    """지운 칸과 상관없는 곳까지 다르게 그려지면 그 측정은 버린다 (함정 PPT 7쪽 '655pt 넘침')."""
    from slideguard.adapters.powerpoint.pixel_bounds import _inside_cell

    cell = Box(100, 200, 140, 40)
    assert _inside_cell(Box(110, 210, 40, 15), cell) == Box(110, 210, 40, 15)
    assert _inside_cell(Box(60, 80, 1240, 450), cell) is None
    assert _inside_cell(None, cell) is None
