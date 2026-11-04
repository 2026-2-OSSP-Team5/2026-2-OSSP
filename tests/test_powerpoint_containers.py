from dataclasses import asdict

import pytest

from slideguard.adapters.powerpoint.properties import read_optional
from slideguard.adapters.powerpoint.session import _plain_snapshot
from slideguard.domain.models import Box, ObjectLocator
from tests.test_powerpoint_text import (
    Collection,
    Properties,
    TextRange,
    extract,
    make_slide,
    measure,
    run,
    shape,
)


def group(shape_id, children):
    item = shape(shape_type=6, shape_id=shape_id)
    item.GroupItems = Collection(children)
    return item


def test_nested_groups_keep_unique_addresses_and_slide_space_measurements():
    first, second = shape(shape_id=7), shape(shape_id=7)
    first.Left, first.Top = 210, 110
    second.Left = 500
    outer = group(100, [group(101, [first])])
    outer.Left, outer.Top = 200, 100
    result = extract(make_slide([outer, group(200, [second])]))
    text = [item for item in result.shapes if item.text]
    assert [item.locator.shape_path for item in text] == [(100, 101, 7), (200, 7)]
    assert len({item.key for item in result.shapes}) == len(result.shapes)
    assert result.shapes[0].key == "1:100"
    assert text[0].box == Box(210, 110, 300, 200)
    assert text[1].box == Box(500, 0, 300, 200)
    assert all(item.rendered_box is not None and item.background is None for item in text)
    assert _plain_snapshot(result) == result


@pytest.mark.parametrize("property_name,value", [("Rotation", 30), ("HorizontalFlip", -1), ("Visible", 0)])
def test_group_transforms_preserve_text_but_mark_unverified_geometry(property_name, value):
    item = group(100, [shape()])
    setattr(item, property_name, value)
    result = extract(make_slide([item])).shapes[1]
    assert result.text == "발표" and result.runs[0].font_size_pt == 24
    assert result.box is None and result.rendered_box is None and result.background is None
    assert any("Ancestor" in note for note in result.notes)


def test_table_cells_use_row_column_addresses_and_deduplicate_merged_positions():
    cell = shape([run("병합 셀")])
    cell.Id = AssertionError("Table cell IDs are not supported by Office")
    table = shape(shape_type=19, shape_id=50)
    table.Table = Properties(
        Rows=Properties(Count=2), Columns=Properties(Count=2), Cell=lambda row, column: Properties(Shape=cell)
    )
    result = extract(make_slide([table]))
    assert len(result.shapes) == 2
    value = result.shapes[1]
    assert value.text == "병합 셀" and value.locator == ObjectLocator((50,), "table_cell", (1, 1))
    assert value.key == "1:50:table_cell:1,1"
    assert value.box is not None and value.runs[0].color is not None


def test_table_inside_placeholder_is_traversed_without_claiming_its_text_is_unsupported():
    item = shape(shape_type=14, shape_id=50)
    item.PlaceholderFormat.ContainedType = 19
    cell = shape([run("자리 표시자 표")])
    item.Table = Properties(
        Rows=Properties(Count=1), Columns=Properties(Count=1), Cell=lambda row, column: Properties(Shape=cell)
    )
    result = extract(make_slide([item])).shapes
    assert result[1].text == "자리 표시자 표" and result[1].locator.kind == "table_cell"
    assert not any("not traversed" in note for note in result[0].notes)


def chart_element(text, left=5):
    return Properties(
        Text=text,
        Left=left,
        Top=10,
        Width=100,
        Height=20,
        Format=Properties(TextFrame2=Properties(TextRange=TextRange([run(text)]))),
    )


def test_chart_titles_axes_and_generated_labels_keep_element_identity():
    title, axis_title, label = chart_element("제목"), chart_element("축"), chart_element("자동 레이블")
    label.Format.TextFrame2.TextRange = Properties(Text="", Font=run("unused").Font)
    point = Properties(HasDataLabel=True, DataLabel=label)
    series = Properties(HasDataLabels=True, Points=lambda: Collection([point]))

    def axis(axis_type, axis_group):
        if (axis_type, axis_group) != (1, 1):
            raise AttributeError("No such axis")
        return Properties(HasTitle=True, AxisTitle=axis_title)

    item = shape(shape_type=3, shape_id=20)
    item.Chart = Properties(
        HasTitle=True,
        ChartTitle=title,
        Axes=axis,
        HasAxis=lambda axis_type, axis_group: (axis_type, axis_group) == (1, 1),
        ChartArea=Properties(Width=300, Height=200),
        SeriesCollection=lambda: Collection([series]),
    )
    result = extract(make_slide([item])).shapes
    assert [value.locator.kind for value in result] == ["shape", "chart_title", "axis_title", "data_label"]
    assert [value.text for value in result[1:]] == ["제목", "축", "자동 레이블"]
    assert result[-1].runs[0].text == "자동 레이블"
    assert result[1].box.x == 5 and result[1].rendered_box is None
    assert result[2].locator.indices == (1, 1) and result[3].locator.indices == (1, 1)
    assert len({value.key for value in result}) == 4


def test_smartart_nodes_do_not_require_internal_shape_ids():
    graphic = Properties(
        Left=30,
        Top=40,
        Width=100,
        Height=60,
        Id=AssertionError("SmartArt shapes do not expose a regular shape ID"),
    )
    node = Properties(TextFrame2=Properties(TextRange=TextRange([run("노드")])), Shapes=Collection([graphic]))
    item = shape(shape_type=24, shape_id=60)
    item.SmartArt = Properties(AllNodes=Collection([node]))
    result = extract(make_slide([item])).shapes[1]
    assert result.locator == ObjectLocator((60,), "smartart_node", (1,))
    assert result.text == "노드" and result.box.x == 30 and result.runs[0].color == (17, 34, 51)
    assert result.rendered_box is None and result.background is None
    assert asdict(result)["locator"]["indices"] == (1,)


@pytest.mark.parametrize("alpha", [0.25, 0.9, 1.0])
def test_transparent_text_is_not_reported_as_opaque_rgb(alpha):
    item = run("투명", rgb=0)
    item.Font.Fill.Transparency = alpha
    value = measure(shape([item])).runs[0]
    assert value.source_color == (0, 0, 0) and value.transparency == alpha
    assert value.color is None and value.font_size_pt == 24 and value.fill_kind == "solid"


def test_mixed_transparency_remains_separate_with_unicode_text():
    first, second = run("😀"), run("한글")
    second.Font.Fill.Transparency = 0.5
    result = measure(shape([first, second]))
    assert [value.transparency for value in result.runs] == [0, 0.5]
    assert result.runs[0].color is not None and result.runs[1].color is None
    assert "".join(value.text for value in result.runs) == "😀한글"


def test_invisible_text_fill_is_distinguished_from_opaque_black():
    value = run("채우기 없음", rgb=0)
    value.Font.Fill.Visible = 0
    result = measure(shape([value])).runs[0]
    assert result.color is None and result.fill_visible is False
    assert result.source_color == (0, 0, 0) and result.transparency == 0


@pytest.mark.parametrize("fill_type", [2, 3, 4, 5, 6, -2])
def test_complex_text_fill_keeps_font_size_without_claiming_rgb(fill_type):
    item = run("복잡한 채우기")
    item.Font.Fill.Type = fill_type
    item.Font.Fill.ForeColor = AssertionError("Complex fills must not be read as a representative RGB")
    result = measure(shape([item]))
    assert result.runs[0].color is None and result.runs[0].transparency is None
    assert result.runs[0].font_size_pt == 24 and result.notes


def test_wrapped_office_not_implemented_is_optional_but_unknown_errors_propagate():
    class WrappedError(Exception):
        hresult = -2147352567
        excepinfo = (0, None, None, None, 0, -2147467263)

    def unavailable():
        raise WrappedError()

    notes = []
    assert read_optional(unavailable, "Optional Office property", notes) is None
    assert notes
    WrappedError.excepinfo = (0, None, None, None, 0, -2147467259)
    with pytest.raises(WrappedError):
        read_optional(unavailable, "Unexpected failure", [])
