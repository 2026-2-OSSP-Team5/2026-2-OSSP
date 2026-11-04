"""Convert PowerPoint objects and addressed container contents to plain snapshots."""

import math
from dataclasses import replace
from typing import Any

from slideguard.domain.models import Box, DeckSnapshot, ObjectLocator, Shape, SlideDetails

from .background import read_background
from .copies import SavedPresentationCopy
from .elements import chart_elements, smartart_nodes, table_cells
from .layers import resolve_backgrounds
from .measurements import measure_shape, optional, paint
from .properties import read_optional, tristate
from .text import TextMeasurement, read_text
from .text_layout import text_spans

# Office MsoShapeType values; no COM library is needed to import this module.
_AUTO_SHAPE, _PLACEHOLDER, _TEXT_BOX = 1, 14, 17
_SUPPORTED_TYPES = {_AUTO_SHAPE, _PLACEHOLDER, _TEXT_BOX}
_TYPE_NAMES = {3: "chart", 6: "group", 13: "picture", 19: "table", 21: "diagram", 24: "SmartArt"}
_LIMITATIONS = (
    "Groups, table cells, chart titles/axis titles/data labels and SmartArt nodes are addressed within one saved revision",
    "Individual chart tick and legend entry glyph bounds are unavailable; complex background samples are approximate",
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


def _shape(
    shape: Any, slide_number: int, slide: Any, shape_count: int, *, locator=None, nested=False
) -> Shape:
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
            if contained_type in (3, 6, 19, 24):
                notes.append(
                    f"Placeholder container content type: {contained_type}; addressed contents are extracted separately"
                )
            else:
                notes.append(
                    f"Unsupported placeholder content type: {contained_type}; content was not traversed"
                )
    elif not supported:
        label = _TYPE_NAMES.get(shape_type, str(shape_type))
        if shape_type in (3, 6, 19, 24):
            notes.append(f"Container shape type: {label}; addressed contents are extracted separately")
        else:
            notes.append(f"Unsupported top-level shape type: {label}; content was not traversed")
    measurement = TextMeasurement()
    background = None
    if supported:
        has_text_frame = read_optional(lambda: shape.HasTextFrame, "Shape.HasTextFrame", notes)
        if type(has_text_frame) is int and has_text_frame == -1:
            measurement = read_text(shape, notes)
            if measurement.text and not nested:
                background = read_background(
                    shape,
                    slide,
                    box=box,
                    rendered_box=measurement.rendered_box,
                    shape_type=shape_type,
                    shape_count=shape_count,
                    notes=notes,
                )
            elif measurement.text:
                notes.append("Nested object background composition is unverified")
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
        locator=locator,
    )


def _layout_verified(item) -> bool:
    notes = []
    values = [
        read_optional(lambda name=name: getattr(item, name), name, notes)
        for name in ("Rotation", "HorizontalFlip", "VerticalFlip", "Visible")
    ]
    rotation, horizontal, vertical, visible = values
    three_d = read_optional(lambda: item.ThreeD.Visible, "Ancestor 3D visibility", notes)
    return (
        type(rotation) in (int, float)
        and math.isfinite(rotation)
        and rotation % 360 == 0
        and type(horizontal) is int
        and horizontal == 0
        and type(vertical) is int
        and vertical == 0
        and type(visible) is int
        and visible == -1
        and type(three_d) is int
        and three_d == 0
    )


def _walk(collection, number, slide, path=(), *, ancestor_verified=True, ancestor_visible=True, depth=0):
    if depth > 64:
        raise ValueError("Group nesting exceeds the supported traversal depth")
    count = _integer(collection.Count, "Shapes.Count")
    seen_ids = set()
    for index in range(1, count + 1):
        item = collection.Item(index)
        shape_id = _integer(item.Id, "Shape.Id")
        if shape_id in seen_ids:
            raise ValueError(f"Duplicate shape ID on slide {number}: {shape_id}")
        seen_ids.add(shape_id)
        address = (*path, shape_id)
        parent = _shape(item, number, slide, count, locator=ObjectLocator(address), nested=bool(path))
        parent = measure_shape(parent, item, ancestor_visible=ancestor_visible)
        if parent.z_order is not None and parent.shape_type in _SUPPORTED_TYPES and parent.supported:
            color, transparency = paint(item, [])
            parent = replace(parent, fill_color=color, fill_transparency=transparency)
        if not ancestor_verified:
            parent = replace(
                parent,
                box=None,
                rendered_box=None,
                content_box=None,
                background=None,
                notes=(*parent.notes, "Ancestor transforms prevent verified slide-space geometry"),
            )
        parent = replace(parent, spans=text_spans(item, parent))
        yield parent
        notes = []
        kind = read_optional(lambda item=item: item.Type, "Shape type", notes)
        if kind == _PLACEHOLDER:
            kind = read_optional(
                lambda item=item: item.PlaceholderFormat.ContainedType, "Placeholder content", notes
            )
        verified = ancestor_verified and _layout_verified(item)
        if kind == 6:
            yield from _walk(
                item.GroupItems,
                number,
                slide,
                address,
                ancestor_verified=verified,
                ancestor_visible=parent.effective_visible,
                depth=depth + 1,
            )
        elif kind == 19:
            yield from table_cells(item, parent, layout_verified=verified)
        elif kind == 3:
            yield from chart_elements(item, parent, layout_verified=verified)
        elif kind == 24:
            yield from smartart_nodes(item, parent, layout_verified=verified)


def extract_snapshot(presentation: Any, saved: SavedPresentationCopy) -> DeckSnapshot:
    setup = presentation.PageSetup
    width = _point(setup.SlideWidth, "PageSetup.SlideWidth", minimum=0)
    height = _point(setup.SlideHeight, "PageSetup.SlideHeight", minimum=0)
    if width == 0 or height == 0:
        raise ValueError("Slide dimensions must be positive")
    slides = presentation.Slides
    slide_count = _integer(slides.Count, "Slides.Count")
    shapes = []
    details = []
    for number in range(1, slide_count + 1):
        slide = slides.Item(number)
        hidden = tristate(
            optional(lambda slide=slide: slide.SlideShowTransition.Hidden, "Hidden slide", []), "Hidden", []
        )
        details.append(SlideDetails(number, hidden=hidden))
        shapes.extend(
            resolve_backgrounds(
                list(
                    _walk(
                        slide.Shapes, number, slide, ancestor_visible=None if hidden is None else not hidden
                    )
                ),
                slide,
            )
        )
    return DeckSnapshot(
        source_path=str(saved.source_path),
        source_sha256=saved.source_sha256,
        slide_count=slide_count,
        width_pt=width,
        height_pt=height,
        shapes=tuple(shapes),
        limitations=_LIMITATIONS,
        slides=tuple(details),
    )
