"""Read geometry and paint facts without applying readability policy."""

import json
from dataclasses import replace

from slideguard.domain.models import Box

from .colors import read_color
from .properties import number, read_optional, tristate

_UNSUPPORTED = (0x80004005, 0x80070057, 0x800A03EC)


def optional(getter, label, notes):
    return read_optional(getter, label, notes, unavailable=_UNSUPPORTED)


def measure_shape(result, item, *, ancestor_visible=True):
    notes = []
    visible = tristate(optional(lambda: item.Visible, "Object visibility", notes), "Visibility", notes)
    effective = False if False in (visible, ancestor_visible) else visible if ancestor_visible else None
    kind = optional(lambda: item.Type, "Object kind", notes)
    role = optional(lambda: item.PlaceholderFormat.Type, "Placeholder role", notes) if kind == 14 else None
    z_order = optional(lambda: item.ZOrderPosition, "Drawing order", notes)
    content = None
    frame_details = ()
    if result.text:
        frame = optional(lambda: item.TextFrame, "Text frame", notes)
        margins = tuple(
            number(optional(lambda name=name: getattr(frame, name), name, notes), name, notes)
            for name in ("MarginLeft", "MarginTop", "MarginRight", "MarginBottom")
        )
        frame_details = tuple(
            (name, value)
            for name in ("AutoSize", "WordWrap", "Orientation", "VerticalAnchor")
            if type(value := optional(lambda name=name: getattr(frame, name), name, notes)) in (int, bool)
        )
        if result.box is not None and None not in margins:
            left, top, right, bottom = margins
            box = result.box
            if box.width >= left + right and box.height >= top + bottom:
                content = Box(box.x + left, box.y + top, box.width - left - right, box.height - top - bottom)
    return replace(
        result,
        visible=visible,
        effective_visible=effective,
        shape_type=kind if type(kind) is int else None,
        placeholder_type=role if type(role) is int else None,
        z_order=z_order if type(z_order) is int else None,
        content_box=content,
        properties=(*frame_details, *_graphic_properties(item, kind)),
    )


def _graphic_properties(item, kind):
    """Only emit graphics evidence when opacity and meaning can be established."""
    result = []
    alt = optional(lambda: item.AlternativeText, "Alternative text", [])
    if kind == 9 or isinstance(alt, str) and alt.strip():
        line = optional(lambda: item.Line, "Graphic line", [])
        if (
            optional(lambda: line.Visible, "Line visibility", []) == -1
            and optional(lambda: line.Transparency, "Line transparency", []) == 0
        ):
            color = read_color(optional(lambda: line.ForeColor, "Line color", []), "Line color", [])
            if color:
                result.append(("meaningful_graphic_color", "%02X%02X%02X" % color))
    if kind == 3:
        from slideguard.checks.contrast import contrast_ratio

        chart = optional(lambda: item.Chart, "Chart", [])
        plot = optional(lambda: chart.PlotArea.Format.Fill, "Plot fill", [])
        if optional(lambda: plot.Visible, "Plot visibility", []) == 0:
            plot = optional(lambda: chart.ChartArea.Format.Fill, "Chart fill", [])
        background = _opaque_color(plot)
        collection = optional(lambda: chart.SeriesCollection(), "Chart series", [])
        count = optional(lambda: collection.Count, "Series count", [])
        series = []
        for index in range(1, min(count, 64) + 1) if type(count) is int else ():
            entry = optional(lambda index=index: collection.Item(index), "Series", [])
            markers = optional(lambda entry=entry: entry.MarkerStyle, "Markers", [])
            labels = optional(lambda entry=entry: entry.HasDataLabels, "Data labels", [])
            dash = optional(lambda entry=entry: entry.Format.Line.DashStyle, "Dash style", [])
            color = optional(lambda entry=entry: entry.Format.Fill.ForeColor.RGB, "Series fill", [])
            fill = optional(lambda entry=entry: entry.Format.Fill, "Series fill", [])
            rgb = _opaque_color(fill)
            chart_type = optional(lambda entry=entry: entry.ChartType, "Series chart type", [])
            bars = chart_type in range(51, 61) if type(chart_type) is int else False
            series.append(
                {
                    "markers": -4142 if bars else markers,
                    "labels": labels,
                    "dash": 1 if bars and rgb else dash,
                    "color": color if type(color) is int else None,
                    "contrast": contrast_ratio(rgb, background) if rgb and background else None,
                }
            )
        # A positive result requires known labels/markers and identical line styles.
        if len(series) >= 2 and all(
            s["labels"] == 0
            and s["markers"] in (-4142, 0)
            and s["dash"] is not None
            and s["color"] is not None
            for s in series
        ):
            if len({s["color"] for s in series}) > 1 and len({s["dash"] for s in series}) == 1:
                result.append(("chart_color_only", True))
        result.append(("chart_series", json.dumps(series, sort_keys=True)))
    return tuple(result)


def _opaque_color(fill):
    if (
        optional(lambda: fill.Visible, "Paint visibility", []) == -1
        and optional(lambda: fill.Type, "Paint type", []) == 1
        and optional(lambda: fill.Transparency, "Paint transparency", []) == 0
    ):
        return read_color(optional(lambda: fill.ForeColor, "Paint color", []), "Paint color", [])
    return None


def paint(item, notes):
    fill = optional(lambda: item.Fill, "Object fill", notes)
    if (
        tristate(optional(lambda: fill.Visible, "Fill visibility", notes), "Fill visibility", notes)
        is not True
    ):
        return None, None
    kind = optional(lambda: fill.Type, "Fill kind", notes)
    if kind != 1:
        return None, None
    alpha = number(optional(lambda: fill.Transparency, "Fill transparency", notes), "Transparency", notes)
    color = read_color(optional(lambda: fill.ForeColor, "Fill color", notes), "Fill color", notes)
    return color, alpha if alpha is not None and 0 <= alpha <= 1 else None
