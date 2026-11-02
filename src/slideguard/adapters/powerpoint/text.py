"""Measure run formatting and verified horizontal text bounds without making judgments."""

from dataclasses import dataclass

from slideguard.domain.models import Box, TextRun

from .colors import read_color
from .properties import number, read_optional, tristate


@dataclass(frozen=True)
class TextMeasurement:
    text: str = ""
    runs: tuple[TextRun, ...] = ()
    rendered_box: Box | None = None


def _font(run, text: str, index: int, notes: list[str]) -> TextRun:
    label = f"Run {index}"
    font = read_optional(lambda: run.Font, f"{label} font", notes)
    size = number(read_optional(lambda: font.Size, f"{label} font size", notes), f"{label} font size", notes)
    if size is not None and size <= 0:
        notes.append(f"{label} font size is mixed or nonpositive")
        size = None
    bold = tristate(read_optional(lambda: font.Bold, f"{label} bold", notes), f"{label} bold", notes)
    color_format = read_optional(lambda: font.Color, f"{label} color", notes)
    color = read_color(color_format, f"{label} color", notes)
    return TextRun(text, font_size_pt=size, color=color, bold=bold)


def _runs(text_range, text: str, notes: list[str]) -> tuple[TextRun, ...]:
    count = read_optional(lambda: text_range.Runs().Count, "Text run count", notes)
    if type(count) is not int or count < 1:
        notes.append("Text run segmentation is unavailable; plain text was preserved")
        return ()
    runs = []
    for index in range(1, count + 1):
        run = read_optional(lambda index=index: text_range.Runs(index, 1), f"Run {index}", notes)
        value = read_optional(lambda run=run: run.Text, f"Run {index} text", notes)
        if type(value) is not str:
            notes.append("Text run coverage could not be verified; partial run segmentation was discarded")
            return ()
        if value:
            runs.append(_font(run, value, index, notes))
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


def read_text(shape, notes: list[str]) -> TextMeasurement:
    frame = read_optional(lambda: shape.TextFrame, "Shape text frame", notes)
    text_range = read_optional(lambda: frame.TextRange, "Shape text range", notes)
    text = read_optional(lambda: text_range.Text, "Shape text", notes)
    if type(text) is not str:
        notes.append("Shape text is not a string; no text was inferred")
        return TextMeasurement()
    if not text:
        notes.append("Empty text; no visible run formatting or rendered bounds were measured")
        return TextMeasurement()
    runs = _runs(text_range, text, notes)
    bounds = _bounds(shape, frame, text_range, notes)
    return TextMeasurement(text, runs, bounds)
