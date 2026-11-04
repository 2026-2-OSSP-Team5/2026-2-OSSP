"""Extract chart legend appearance without assigning a series to a rendered entry."""

from math import isfinite
from types import SimpleNamespace

from slideguard.domain.models import Box, ObjectEvidence

from .colors import read_color
from .measurements import optional
from .text import read_run


def _properties(item, fields, notes):
    results = []
    for key, name in fields:
        value = optional(lambda name=name: getattr(item, name), f"Legend {name}", notes)
        if type(value) in (str, bool, int) or (type(value) is float and isfinite(value)):
            results.append((key, value))
    return tuple(results)


def _rgb_properties(name, color):
    if color is None:
        return ()
    return tuple((f"{name}_{channel}", value) for channel, value in zip("rgb", color, strict=True))


def _packed_rgb(value):
    if type(value) is int and 0 <= value <= 0xFFFFFF:
        return value & 255, (value >> 8) & 255, (value >> 16) & 255
    return None


def _paint(element, notes):
    style = optional(lambda: element.Format, "Legend format", notes)
    fill = optional(lambda: style.Fill, "Legend fill", notes)
    line = optional(lambda: style.Line, "Legend line", notes)
    results = [
        *_properties(
            fill,
            (
                ("fill_kind", "Type"),
                ("fill_visible", "Visible"),
                ("fill_transparency", "Transparency"),
                ("fill_pattern", "Pattern"),
            ),
            notes,
        ),
        *_properties(
            line,
            (
                ("line_visible", "Visible"),
                ("line_weight_pt", "Weight"),
                ("line_dash", "DashStyle"),
                ("line_style", "Style"),
                ("line_transparency", "Transparency"),
            ),
            notes,
        ),
    ]
    for prefix, item, name in (
        ("fill_foreground", fill, "ForeColor"),
        ("fill_background", fill, "BackColor"),
        ("line_foreground", line, "ForeColor"),
    ):
        color = optional(lambda item=item, name=name: getattr(item, name), prefix, notes)
        # Some chart types return a Font/Format property but reject individual color getters.
        rgb = optional(lambda color=color, prefix=prefix: read_color(color, prefix, notes), prefix, notes)
        results.extend(_rgb_properties(prefix, rgb))
    return tuple(results)


def _font(element, notes):
    font = optional(lambda: element.Font, "Legend font", notes)
    run = optional(lambda: read_run(SimpleNamespace(Font=font), "", 1, notes), "Legend font values", notes)
    props = _properties(
        font,
        (
            ("font_name", "Name"),
            ("italic", "Italic"),
            ("underline", "Underline"),
            ("strikethrough", "Strikethrough"),
        ),
        notes,
    )
    frame = optional(lambda: element.Format.TextFrame2, "Legend text frame", notes)
    modern_font = optional(lambda: frame.TextRange.Font, "Legend text range font", notes)
    modern = (
        optional(
            lambda: read_run(SimpleNamespace(Font=modern_font), "", 1, notes, modern=True),
            "Legend text paint",
            notes,
        )
        if modern_font is not None
        else None
    )
    props = (
        *props,
        *_properties(frame, (("text_orientation", "Orientation"), ("text_wrap", "WordWrap")), notes),
    )
    if modern is not None:
        props = (
            *props,
            *(
                (name, value)
                for name, value in (
                    ("text_fill_kind", modern.fill_kind),
                    ("text_fill_visible", modern.fill_visible),
                    ("text_transparency", modern.transparency),
                )
                if value is not None
            ),
            *_rgb_properties("text_fill", modern.source_color),
        )
    if run is None:
        return props
    return (
        *props,
        *(
            (name, value)
            for name, value in (("font_size_pt", run.font_size_pt), ("bold", run.bold))
            if value is not None
        ),
        *_rgb_properties("source_color", run.source_color),
    )


def _box(item, parent, sx, sy):
    if parent.box is None or sx is None or sy is None:
        return None
    values = dict(_properties(item, tuple((name, name) for name in ("Left", "Top", "Width", "Height")), []))
    if len(values) != 4 or any(type(value) not in (int, float) for value in values.values()):
        return None
    if values["Width"] <= 0 or values["Height"] <= 0:
        return None
    return Box(
        parent.box.x + values["Left"] * sx,
        parent.box.y + values["Top"] * sy,
        values["Width"] * sx,
        values["Height"] * sy,
    )


def legend_appearance(legend, parent, sx, sy):
    notes = []
    properties = (
        *_properties(
            legend, (("include_in_layout", "IncludeInLayout"), ("auto_scale_font", "AutoScaleFont")), notes
        ),
        *_font(legend, notes),
        *_paint(legend, notes),
    )
    collection = optional(lambda: legend.LegendEntries(), "Legend entries", notes)
    count = optional(lambda: collection.Count, "Legend entry count", notes)
    records = []
    if type(count) is not int:
        return properties, ()
    for index in range(1, min(count, 256) + 1):
        entry = optional(lambda index=index: collection.Item(index), "Legend entry", notes)
        if entry is None:
            records.append(ObjectEvidence("chart_legend_entry", f"com:{index}", "unavailable"))
            continue
        key = optional(lambda entry=entry: entry.LegendKey, "Legend key", notes)
        children = ()
        if key is not None:
            key_properties = [
                *_properties(
                    key,
                    (("marker_style", "MarkerStyle"), ("marker_size_pt", "MarkerSize"), ("smooth", "Smooth")),
                    notes,
                ),
                *_paint(key, notes),
            ]
            for name in ("MarkerForegroundColor", "MarkerBackgroundColor"):
                color = optional(lambda key=key, name=name: getattr(key, name), name, notes)
                key_properties.extend(_rgb_properties(name, _packed_rgb(color)))
            children = (
                ObjectEvidence(
                    "chart_legend_key",
                    f"com:{index}",
                    "partial",
                    box=_box(key, parent, sx, sy),
                    properties=tuple(key_properties),
                ),
            )
        records.append(
            ObjectEvidence(
                "chart_legend_entry",
                f"com:{index}",
                "partial",
                box=_box(entry, parent, sx, sy),
                properties=_font(entry, notes),
                children=children,
            )
        )
    if count > 256:
        records.append(ObjectEvidence("chart_legend_entry", "com:truncated", "partial"))
    return (*properties, ("entries", count)), tuple(records)
