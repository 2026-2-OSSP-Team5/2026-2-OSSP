"""Read chart label groups separately from authored chart text runs."""

from dataclasses import replace
from types import SimpleNamespace

from slideguard.domain.models import Box, Measurement, ObjectLocator, Shape

from .legend import legend_appearance
from .measurements import optional
from .text import read_run


def chart_label(parent, element, kind, indices, *, text="", visible=True, box=None, properties=()):
    notes = []
    font = optional(lambda: element.Font, "Chart label font", notes)
    run = read_run(SimpleNamespace(Font=font), text, 1, notes)
    if kind == "chart_tick_labels":
        notes.append("PowerPoint exposes the axis label group, without individual glyph positions")
    effective = parent.effective_visible if visible else False
    return Shape(
        parent.slide_number,
        parent.shape_id,
        f"{parent.name}/{kind}/{','.join(map(str, indices))}",
        box,
        text=text,
        runs=(run,) if font is not None and visible else (),
        supported=True,
        notes=tuple(notes),
        locator=ObjectLocator(parent.locator.shape_path, kind, indices),
        visible=visible,
        effective_visible=effective,
        properties=properties,
        measurements=(Measurement("text_bounds", "unsupported", "Chart label group geometry"),),
    )


def tick_labels(parent, axis, axis_type, axis_group):
    position = optional(lambda: axis.TickLabelPosition, "Tick label position", [])
    labels = optional(lambda: axis.TickLabels, "Tick labels", [])
    if labels is None or position is None:
        return None
    properties = [("position", position)]
    for name in ("NumberFormat", "Orientation"):
        value = optional(lambda name=name: getattr(labels, name), name, [])
        if type(value) in (str, int, float):
            properties.append((name, value))
    return chart_label(
        parent,
        labels,
        "chart_tick_labels",
        (axis_type, axis_group),
        visible=position != -4142,
        properties=tuple(properties),
    )


def legend_label(parent, chart, sx, sy, optional_box):
    if not optional(lambda: chart.HasLegend, "Legend presence", []):
        return None
    legend = optional(lambda: chart.Legend, "Legend", [])
    if legend is None:
        return None
    box = optional_box(legend, [])
    if box is not None and sx is not None and sy is not None and parent.box is not None:
        box = Box(parent.box.x + box.x * sx, parent.box.y + box.y * sy, box.width * sx, box.height * sy)
    else:
        box = None
    position = optional(lambda: legend.Position, "Legend position", [])
    properties, records = legend_appearance(legend, parent, sx, sy)
    if type(position) is int:
        properties = (("position", position), *properties)
    result = chart_label(parent, legend, "chart_legend", (), box=box, properties=properties)
    return replace(result, evidence=records)
