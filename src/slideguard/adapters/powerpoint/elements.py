"""Read table, chart and SmartArt contents without modifying or activating them."""

from dataclasses import replace
from types import SimpleNamespace

from slideguard.domain.models import Box, ObjectLocator, Shape

from .chart_text import legend_label, tick_labels
from .measurements import measure_shape, optional, paint
from .properties import indexed_property, number, read_optional
from .text import TextMeasurement, read_range, read_run, read_text
from .text_layout import text_spans


def optional_box(item, notes: list[str]) -> Box | None:
    values = []
    for name in ("Left", "Top", "Width", "Height"):
        raw = read_optional(lambda name=name: getattr(item, name), f"Element {name}", notes)
        values.append(number(raw, name, notes, minimum=0 if name in ("Width", "Height") else None))
    return None if any(value is None for value in values) else Box(*values)


def _element(parent: Shape, kind: str, indices: tuple[int, ...], box, measurement, notes) -> Shape:
    return Shape(
        parent.slide_number,
        parent.shape_id,
        f"{parent.name}/{kind}/{','.join(map(str, indices))}",
        box,
        text=measurement.text,
        runs=measurement.runs,
        rendered_box=measurement.rendered_box,
        supported=True,
        effective_visible=parent.effective_visible,
        notes=tuple(notes),
        locator=ObjectLocator(parent.locator.shape_path, kind, indices),
    )


def table_cells(item, parent: Shape, *, layout_verified: bool):
    table = item.Table
    seen = set()
    for row in range(1, table.Rows.Count + 1):
        for column in range(1, table.Columns.Count + 1):
            notes = []
            cell = read_optional(
                lambda row=row, column=column: table.Cell(row, column).Shape,
                "Table cell",
                notes,
                unavailable=(0x80070057,),
            )
            if cell is None:
                continue
            box = optional_box(cell, notes)
            # Covered positions of a merged cell return the same cell rectangle.
            if box is not None and box in seen:
                continue
            if box is not None:
                seen.add(box)
            measurement = read_text(cell, notes, layout_shape=item)
            if not layout_verified:
                box = None
                measurement = replace(measurement, rendered_box=None)
                notes.append("Ancestor transforms prevent verified slide-space cell geometry")
            notes.append("Cell background/table-style/border composition is not confirmed")
            result = measure_shape(
                _element(parent, "table_cell", (row, column), box, measurement, notes),
                cell,
                ancestor_visible=parent.effective_visible,
            )
            color, transparency = paint(cell, notes)
            yield replace(
                result, fill_color=color, fill_transparency=transparency, spans=text_spans(cell, result)
            )


def _chart_element(element, parent, kind, indices, sx, sy):
    notes = []
    text = read_optional(lambda: element.Text, "Chart element text", notes)
    box = optional_box(element, notes)
    if box is not None and parent.box is not None and sx is not None and sy is not None:
        box = Box(parent.box.x + box.x * sx, parent.box.y + box.y * sy, box.width * sx, box.height * sy)
    else:
        box = None
    text_range = read_optional(
        lambda: element.Format.TextFrame2.TextRange,
        "Chart text range",
        notes,
        unavailable=(0x80004005, 0x80070057),
    )
    measurement = read_range(text_range, notes)
    if type(text) is str and measurement.text != text:
        # Automatically generated labels can expose their visible Text while their
        # rich-text range is empty. Preserve that text with independently read font.
        font = read_optional(lambda: text_range.Font, "Generated chart label font", notes)
        run = read_run(SimpleNamespace(Font=font), text, 1, notes, modern=True) if text else None
        measurement = TextMeasurement(text, (run,) if run is not None else ())
        notes.append("Generated chart text has no matching rich-text segmentation")
    notes.append("Chart element box is not a measured glyph box; background composition is unknown")
    return _element(parent, kind, indices, box, measurement, notes)


def chart_elements(item, parent: Shape, *, layout_verified: bool):
    chart = item.Chart
    # Measuring ChartArea can initialize Office's automatic chart layout and
    # synthesize a default title on some pie charts. Enumerate existing titles
    # from the state before those layout-triggering getters are evaluated.
    has_title = chart.HasTitle
    sx = sy = None
    if layout_verified and parent.box is not None:
        notes = []
        width = number(
            read_optional(lambda: chart.ChartArea.Width, "Chart width", notes), "Chart width", notes
        )
        height = number(
            read_optional(lambda: chart.ChartArea.Height, "Chart height", notes), "Chart height", notes
        )
        if width is not None and width > 0 and height is not None and height > 0:
            sx, sy = parent.box.width / width, parent.box.height / height
    if has_title:
        yield _chart_element(chart.ChartTitle, parent, "chart_title", (), sx, sy)
    chart_type = optional(lambda: chart.ChartType, "Chart type", [])
    # Pie and doughnut charts have no axes. The late-bound HasAxis getter
    # can expose default axes when layout is initialized by an earlier getter.
    axis_types = () if chart_type in {5, -4102, 68, 69, 70, 71, 80, -4120} else (1, 2, 3)
    for axis_type in axis_types:
        for axis_group in (1, 2):
            notes = []
            present = read_optional(
                lambda axis_type=axis_type, axis_group=axis_group: indexed_property(
                    chart, "HasAxis", axis_type, axis_group
                ),
                "Chart axis presence",
                notes,
            )
            if not present:
                continue
            axis = read_optional(
                lambda axis_type=axis_type, axis_group=axis_group: chart.Axes(axis_type, axis_group),
                "Optional chart axis",
                notes,
                unavailable=(0x800A03EC, 0x80070057, 0x80004005, 0x8002000B),
            )
            has_title = read_optional(
                lambda axis=axis: axis.HasTitle,
                "Axis title presence",
                notes,
                unavailable=(0x80004003, 0x800A03EC, 0x80004005),
            )
            labels = tick_labels(parent, axis, axis_type, axis_group)
            if labels is not None:
                yield labels
            if has_title:
                yield _chart_element(axis.AxisTitle, parent, "axis_title", (axis_type, axis_group), sx, sy)
    legend = legend_label(parent, chart, sx, sy, optional_box)
    if legend is not None:
        yield legend
    series = chart.SeriesCollection()
    for series_index in range(1, series.Count + 1):
        current = series.Item(series_index)
        points = current.Points()
        for point_index in range(1, points.Count + 1):
            point = points.Item(point_index)
            if point.HasDataLabel:
                yield _chart_element(
                    point.DataLabel, parent, "data_label", (series_index, point_index), sx, sy
                )


def smartart_nodes(item, parent: Shape, *, layout_verified: bool):
    nodes = item.SmartArt.AllNodes
    for index in range(1, nodes.Count + 1):
        node = nodes.Item(index)
        notes = []
        measurement = read_range(node.TextFrame2.TextRange, notes)
        shapes = node.Shapes
        boxes = [optional_box(shapes.Item(position), notes) for position in range(1, shapes.Count + 1)]
        box = None
        if layout_verified and boxes and all(value is not None for value in boxes):
            x, y = min(value.x for value in boxes), min(value.y for value in boxes)
            right = max(value.x + value.width for value in boxes)
            bottom = max(value.y + value.height for value in boxes)
            box = Box(x, y, right - x, bottom - y)
        notes.append(
            "SmartArt node box encloses its associated shapes; glyph bounds/background are unverified"
        )
        yield _element(parent, "smartart_node", (index,), box, measurement, notes)
