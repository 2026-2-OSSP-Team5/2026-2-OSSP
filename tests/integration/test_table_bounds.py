"""Table-cell geometry must follow the actual slide position, including merged cells."""

from hashlib import sha256

import pytest
from pptx import Presentation
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Pt

from slideguard.adapters.powerpoint.reader import PowerPointDeckReader
from slideguard.adapters.powerpoint.session import PowerPointReadSession
from slideguard.checks.layout import LayoutRule
from slideguard.domain.policy import AnalysisPolicy

pytestmark = pytest.mark.powerpoint


def test_rendered_table_text_uses_slide_space_on_every_slide(powerpoint_environment, tmp_path):
    path = tmp_path / "table-bounds.pptx"
    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(960), Pt(720)
    for left, top in ((80, 150), (400, 400)):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        table = slide.shapes.add_table(3, 2, Pt(left), Pt(top), Pt(400), Pt(180)).table
        table.cell(0, 0).merge(table.cell(0, 1))
        for row, column, text, anchor in (
            (0, 0, "Merged header", MSO_ANCHOR.TOP),
            (1, 0, "Middle left", MSO_ANCHOR.MIDDLE),
            (1, 1, "Middle right", MSO_ANCHOR.MIDDLE),
            (2, 0, "Bottom left", MSO_ANCHOR.BOTTOM),
            (2, 1, "Bottom right", MSO_ANCHOR.BOTTOM),
        ):
            cell = table.cell(row, column)
            cell.text = text
            cell.vertical_anchor = anchor
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(18)
    deck.save(path)
    original = path.read_bytes()

    snapshot = PowerPointDeckReader(PowerPointReadSession(), render=True).read(path)

    assert path.read_bytes() == original
    assert snapshot.source_sha256 == sha256(original).hexdigest()
    cells = [shape for shape in snapshot.shapes if shape.locator.kind == "table_cell"]
    assert len(cells) == 10
    for cell in cells:
        assert cell.effective_visible is True
        box, text = cell.box, cell.rendered_box
        assert box is not None and text is not None
        assert text.x >= box.x - 1 and text.y >= box.y - 1
        assert text.x + text.width <= box.x + box.width + 1
        assert text.y + text.height <= box.y + box.height + 1
        assert all(span.box is None for span in cell.spans)
    findings = LayoutRule().evaluate(snapshot, AnalysisPolicy())
    assert not any(
        f.rule_id in {"layout.margin", "layout.overflow"} and f.severity == "warning" for f in findings
    )
