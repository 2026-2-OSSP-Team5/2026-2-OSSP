"""Generated, editable container examples; no user presentation is required."""

from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt


def create_expanded_deck(path):
    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(960), Pt(720)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.name = "nested-group"
    inner = group.shapes.add_group_shape()
    inner.name = "inner-group"
    child = inner.shapes.add_textbox(Pt(10), Pt(20), Pt(240), Pt(70))
    child.name = "group-text"
    child.text = "그룹 내부 텍스트"
    child.text_frame.paragraphs[0].runs[0].font.size = Pt(24)
    inner.shapes.add_textbox(Pt(10), Pt(110), Pt(240), Pt(50)).text = "안쪽 형제"
    group.shapes.add_textbox(Pt(300), Pt(20), Pt(100), Pt(50)).text = "바깥 형제"
    group.left, group.top = Pt(100), Pt(70)

    table = slide.shapes.add_table(2, 2, Pt(50), Pt(220), Pt(400), Pt(100))
    table.name = "merged-table"
    table.table.cell(0, 0).text = "병합 셀"
    table.table.cell(0, 0).merge(table.table.cell(0, 1))
    table.table.cell(1, 0).text = "아래 왼쪽"
    table.table.cell(1, 1).text = "아래 오른쪽"
    data = ChartData()
    data.categories = ["첫째", "둘째"]
    data.add_series("계열", [1, 2])
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Pt(480), Pt(50), Pt(430), Pt(300), data)
    chart.name = "text-chart"

    slide = deck.slides.add_slide(deck.slide_layouts[6])
    for name, text, y in (("transparent", "90% 투명", 50), ("mixed-alpha", "😀AB", 180)):
        item = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, Pt(50), Pt(y), Pt(500), Pt(100))
        item.name = name
        item.fill.solid()
        item.fill.fore_color.rgb = RGBColor(255, 255, 255)
        item.line.fill.background()
        item.text_frame.auto_size = MSO_AUTO_SIZE.NONE
        item.text = text
        font = item.text_frame.paragraphs[0].runs[0].font
        font.size, font.bold, font.color.rgb = Pt(24), False, RGBColor(0, 0, 0)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_chart(XL_CHART_TYPE.PIE, Pt(50), Pt(50), Pt(500), Pt(400), data).name = "pie-chart"
    deck.save(path)
