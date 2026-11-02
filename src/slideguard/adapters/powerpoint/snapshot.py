"""Convert top-level PowerPoint COM objects to the existing plain snapshot models."""

import math
from typing import Any

from slideguard.domain.models import Box, DeckSnapshot, Shape

from .background import read_background
from .copies import SavedPresentationCopy
from .properties import read_optional
from .text import TextMeasurement, read_text

# Office MsoShapeType values; no COM library is needed to import this module.
_AUTO_SHAPE, _PLACEHOLDER, _TEXT_BOX = 1, 14, 17
_SUPPORTED_TYPES = {_AUTO_SHAPE, _PLACEHOLDER, _TEXT_BOX}
_TYPE_NAMES = {3: "chart", 6: "group", 13: "picture", 19: "table", 21: "diagram", 24: "SmartArt"}
_LIMITATIONS = (
    "Only top-level text boxes, AutoShapes and text placeholders are supported; other contents are not traversed",
    "COM measurements are not pixel rendering; unresolved properties remain unknown and are described in shape notes",
    "Rendered bounds require verified horizontal, unrotated, unflipped, 2D visible text",
    "Background RGB requires confirmed uniform opaque coverage; complex layers and inherited backgrounds are not composed",
)


def _integer(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _point(value, label: str, *, minimum: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite point measurement")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return float(value)


def _shape(shape: Any, slide_number: int, slide: Any, shape_count: int) -> Shape:
    shape_id = _integer(shape.Id, "Shape.Id")
    box = Box(
        _point(shape.Left, "Shape.Left"),
        _point(shape.Top, "Shape.Top"),
        _point(shape.Width, "Shape.Width", minimum=0),
        _point(shape.Height, "Shape.Height", minimum=0),
    )
    notes = []
    name = read_optional(lambda: shape.Name, "Shape.Name", notes)
    if type(name) is not str:
        notes.append("Shape.Name is not a string; no name was inferred")
        name = ""
    shape_type = read_optional(lambda: shape.Type, "Shape.Type", notes)
    if type(shape_type) is not int:
        shape_type = None
    supported = shape_type in _SUPPORTED_TYPES
    if shape_type == _PLACEHOLDER:
        contained_type = read_optional(
            lambda: shape.PlaceholderFormat.ContainedType, "Placeholder type", notes
        )
        supported = type(contained_type) is int and contained_type in _SUPPORTED_TYPES
        if not supported:
            notes.append(f"Unsupported placeholder content type: {contained_type}; content was not traversed")
    elif not supported:
        label = _TYPE_NAMES.get(shape_type, str(shape_type))
        notes.append(f"Unsupported top-level shape type: {label}; content was not traversed")
    measurement = TextMeasurement()
    background = None
    if supported:
        has_text_frame = read_optional(lambda: shape.HasTextFrame, "Shape.HasTextFrame", notes)
        if type(has_text_frame) is int and has_text_frame == -1:
            measurement = read_text(shape, notes)
            if measurement.text:
                background = read_background(
                    shape,
                    slide,
                    box=box,
                    rendered_box=measurement.rendered_box,
                    shape_type=shape_type,
                    shape_count=shape_count,
                    notes=notes,
                )
        elif shape_type == _PLACEHOLDER and type(has_text_frame) is int and has_text_frame == 0:
            supported = False
            notes.append("The placeholder has no confirmed text frame")
        elif has_text_frame != 0:
            notes.append("The presence of a text frame could not be confirmed")
    return Shape(
        slide_number,
        shape_id,
        name,
        box,
        text=measurement.text,
        runs=measurement.runs,
        background=background,
        rendered_box=measurement.rendered_box,
        supported=supported,
        notes=tuple(notes),
    )


def extract_snapshot(presentation: Any, saved: SavedPresentationCopy) -> DeckSnapshot:
    setup = presentation.PageSetup
    width = _point(setup.SlideWidth, "PageSetup.SlideWidth", minimum=0)
    height = _point(setup.SlideHeight, "PageSetup.SlideHeight", minimum=0)
    if width == 0 or height == 0:
        raise ValueError("Slide dimensions must be positive")
    slides = presentation.Slides
    slide_count = _integer(slides.Count, "Slides.Count")
    shapes = []
    for number in range(1, slide_count + 1):
        slide = slides.Item(number)
        collection = slide.Shapes
        shape_count = _integer(collection.Count, "Shapes.Count")
        seen_ids = set()
        for index in range(1, shape_count + 1):
            shape = _shape(collection.Item(index), number, slide, shape_count)
            if shape.shape_id in seen_ids:
                raise ValueError(f"Duplicate shape ID on slide {number}: {shape.shape_id}")
            seen_ids.add(shape.shape_id)
            shapes.append(shape)
    return DeckSnapshot(
        source_path=str(saved.source_path),
        source_sha256=saved.source_sha256,
        slide_count=slide_count,
        width_pt=width,
        height_pt=height,
        shapes=tuple(shapes),
        limitations=_LIMITATIONS,
    )
