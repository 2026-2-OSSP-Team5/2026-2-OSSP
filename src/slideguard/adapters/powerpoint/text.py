"""Measure run formatting and verified horizontal text bounds without making judgments."""

from dataclasses import dataclass

from slideguard.domain.models import Box, TextRun

from .colors import read_color
from .properties import indexed_property, number, read_optional, tristate


@dataclass(frozen=True)
class TextMeasurement:
    text: str = ""
    runs: tuple[TextRun, ...] = ()
    rendered_box: Box | None = None


def read_run(run, text: str, index: int, notes: list[str], *, modern: bool = False) -> TextRun:
    label = f"Run {index}"
    font = read_optional(lambda: run.Font, f"{label} font", notes)
    size = number(read_optional(lambda: font.Size, f"{label} font size", notes), f"{label} font size", notes)
    if size is not None and size <= 0:
        notes.append(f"{label} font size is mixed or nonpositive")
        size = None
    bold = tristate(read_optional(lambda: font.Bold, f"{label} bold", notes), f"{label} bold", notes)
    if modern:
        fill = read_optional(lambda: font.Fill, f"{label} text fill", notes)
        fill_type = read_optional(lambda: fill.Type, f"{label} fill type", notes)
        kinds = {1: "solid", 2: "pattern", 3: "gradient", 4: "texture", 5: "none", 6: "picture"}
        kind = kinds.get(fill_type, "unknown") if type(fill_type) is int else "unknown"
        visible = tristate(
            read_optional(lambda: fill.Visible, f"{label} fill visibility", notes), label, notes
        )
        source_color = None
        if kind == "solid":
            color_format = read_optional(lambda: fill.ForeColor, f"{label} color", notes)
            source_color = read_color(color_format, f"{label} color", notes)
        transparency = (
            number(
                read_optional(lambda: fill.Transparency, f"{label} transparency", notes),
                f"{label} transparency",
                notes,
                minimum=0,
            )
            if kind == "solid"
            else None
        )
        if transparency is not None and transparency > 1:
            transparency = None
        color = source_color if visible is True and kind == "solid" and transparency == 0 else None
        if color is None:
            notes.append(f"{label} has transparent, non-solid or unverified text fill; opaque RGB is unknown")
        return TextRun(text, size, color, bold, source_color, transparency, kind, visible)
    notes.append(f"{label} text fill/transparency is unavailable; opaque RGB is unknown")
    color_format = read_optional(lambda: font.Color, f"{label} color", notes)
    return TextRun(text, size, None, bold, read_color(color_format, label, notes))


def _runs(text_range, text: str, notes: list[str], *, modern: bool = False) -> tuple[TextRun, ...]:
    # Office TextRange2 has no Count property. Runs(i) clamps to the last run,
    # so stop on exact text coverage and reject repeated source positions.
    count = (
        len(text.encode("utf-16-le")) // 2
        if modern
        else read_optional(lambda: text_range.Runs().Count, "Text run count", notes)
    )
    if type(count) is not int or count < 1:
        notes.append("Text run segmentation is unavailable; plain text was preserved")
        return ()
    runs = []
    previous_start = None
    for index in range(1, count + 1):
        run = read_optional(
            lambda index=index: (
                indexed_property(text_range, "Runs", index, 1) if modern else text_range.Runs(index, 1)
            ),
            f"Run {index}",
            notes,
        )
        value = read_optional(lambda run=run: run.Text, f"Run {index} text", notes)
        if type(value) is not str:
            notes.append("Text run coverage could not be verified; partial run segmentation was discarded")
            return ()
        if modern:
            start = read_optional(lambda run=run: run.Start, f"Run {index} start", notes)
            if start is not None and previous_start is not None and start <= previous_start:
                notes.append("Text run traversal repeated a source position; segmentation was discarded")
                return ()
            previous_start = start
        if value:
            runs.append(read_run(run, value, index, notes, modern=modern))
        combined = "".join(item.text for item in runs)
        if modern and combined == text:
            return tuple(runs)
        if modern and not text.startswith(combined):
            break
    if "".join(run.text for run in runs) != text:
        notes.append("Text runs do not reproduce the full text; unverified segmentation was discarded")
        return ()
    return tuple(runs)


def _bounds(shape, frame, text_range, notes: list[str]) -> Box | None:
    rotation = number(read_optional(lambda: shape.Rotation, "Shape rotation", notes), "Shape rotation", notes)
    orientation = read_optional(lambda: frame.Orientation, "Text orientation", notes)
    visible = tristate(
        read_optional(lambda: shape.Visible, "Shape visibility", notes), "Shape visibility", notes
    )
    horizontal_flip = tristate(
        read_optional(lambda: shape.HorizontalFlip, "Horizontal flip", notes), "Horizontal flip", notes
    )
    vertical_flip = tristate(
        read_optional(lambda: shape.VerticalFlip, "Vertical flip", notes), "Vertical flip", notes
    )
    three_dimensional = tristate(
        read_optional(lambda: shape.ThreeD.Visible, "3D visibility", notes), "3D visibility", notes
    )
    if (
        rotation is None
        or rotation % 360 != 0
        or type(orientation) is not int
        or orientation != 1
        or visible is not True
        or horizontal_flip is not False
        or vertical_flip is not False
        or three_dimensional is not False
    ):
        notes.append(
            "Rendered bounds are unverified for rotated, vertical, flipped, 3D, hidden or unknown text layout"
        )
        return None
    values = []
    for name in ("BoundLeft", "BoundTop", "BoundWidth", "BoundHeight"):
        value = read_optional(lambda name=name: getattr(text_range, name), f"Text {name}", notes)
        values.append(
            number(value, f"Text {name}", notes, minimum=0 if name in ("BoundWidth", "BoundHeight") else None)
        )
    if any(value is None for value in values):
        notes.append("Rendered bounds are incomplete; the shape box was not substituted")
        return None
    return Box(*values)


def read_range(text_range, notes: list[str], *, modern: bool = True) -> TextMeasurement:
    """Read text/formatting without assuming this range has slide-space bounds."""
    text = read_optional(lambda: text_range.Text, "Text range content", notes)
    if type(text) is not str:
        notes.append("Text range content is unavailable")
        return TextMeasurement()
    if not text:
        return TextMeasurement()
    return TextMeasurement(text, _runs(text_range, text, notes, modern=modern))


def read_text(shape, notes: list[str], *, layout_shape=None) -> TextMeasurement:
    frame = read_optional(lambda: shape.TextFrame, "Shape text frame", notes)
    text_range = read_optional(lambda: frame.TextRange, "Shape text range", notes)
    text = read_optional(lambda: text_range.Text, "Shape text", notes)
    if type(text) is not str:
        notes.append("Shape text is not a string; no text was inferred")
        return TextMeasurement()
    if not text:
        notes.append("Empty text; no visible run formatting or rendered bounds were measured")
        return TextMeasurement()
    modern_range = read_optional(lambda: shape.TextFrame2.TextRange, "Modern text range", notes)
    modern_text = read_optional(lambda: modern_range.Text, "Modern text content", notes)
    if type(modern_text) is str and modern_text == text:
        runs = _runs(modern_range, text, notes, modern=True)
    else:
        runs = _runs(text_range, text, notes)
    bounds = _bounds(layout_shape or shape, frame, text_range, notes)
    return TextMeasurement(text, runs, bounds)
