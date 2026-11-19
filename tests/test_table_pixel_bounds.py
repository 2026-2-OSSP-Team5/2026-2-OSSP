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
