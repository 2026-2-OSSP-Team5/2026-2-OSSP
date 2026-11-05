"""Measure rendered pixel differences on the private PowerPoint copy."""

from dataclasses import replace

from PIL import Image, ImageChops

from slideguard.domain.models import Box, Measurement

from .measurements import optional


def export_slide(slide, target, width, height):
    notes = []
    try:
        optional(lambda: slide.Export(str(target), "PNG", width, height), "Slide PNG export", notes)
        if notes or not target.is_file():
            return None
        with Image.open(target) as source:
            return source.convert("RGB")
    finally:
        target.unlink(missing_ok=True)


def _pixel_box(before, after, sx, sy):
    if after is None or before.size != after.size:
        return None
    bounds = ImageChops.difference(before, after).getbbox()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return Box(left / sx, top / sy, (right - left) / sx, (bottom - top) / sy)


def _positions(slide, path):
    collection = slide.Shapes
    result = []
    for shape_id in path:
        for index in range(1, collection.Count + 1):
            item = collection.Item(index)
            if item.Id == shape_id:
                result.append(index)
                if len(result) < len(path):
                    collection = item.GroupItems
                break
        else:
            return ()
    return tuple(result)


def _item(slide, positions):
    item = slide.Shapes.Item(positions[0])
    for position in positions[1:]:
        item = item.GroupItems.Item(position)
    return item


def _geometry(item):
    return item.Left, item.Top, item.Width, item.Height


def _text_pixels(slide, positions, render, sx, sy):
    notes = []
    duplicate = optional(lambda: slide.Duplicate().Item(1), "Temporary probe slide", notes)
    if duplicate is None:
        return None
    try:
        item = _item(duplicate, positions)
        frame = optional(lambda: item.TextFrame, "Probe text frame", notes)
        # Clearing text is safe as a comparison only with a fixed shape size.
        if optional(lambda: frame.AutoSize, "Probe text frame AutoSize", notes) != 0:
            return None
        geometry = _geometry(item)
        before = render(duplicate)
        if before is None:
            return None
        optional(lambda: setattr(frame.TextRange, "Text", ""), "Clear text in probe slide", notes)
        if notes or _geometry(item) != geometry:
            return None
        return _pixel_box(before, render(duplicate), sx, sy)
    finally:
        duplicate.Delete()


def measure_pixels(shape, slide, before, render, sx, sy):
    if shape.effective_visible is not True or not shape.locator or shape.locator.kind != "shape":
        return shape
    positions = _positions(slide, shape.locator.shape_path)
    if not positions:
        return shape
    item = _item(slide, positions)
    probe_notes = []
    if optional(lambda: item.Visible, "Probe shape visibility", probe_notes) != -1:
        return shape
    try:
        optional(lambda: setattr(item, "Visible", 0), "Hide probe shape", probe_notes)
        object_box = None if probe_notes else _pixel_box(before, render(slide), sx, sy)
    finally:
        if item.Visible != -1:
            item.Visible = -1
    text_box = _text_pixels(slide, positions, render, sx, sy) if shape.text else None
    notes = [
        Measurement(
            "visible_object_box",
            "measured" if object_box else "unavailable",
            "Slide PNG difference with object hidden; visible pixels including effects",
        ),
    ]
    if shape.text:
        notes.append(
            Measurement(
                "visible_text_box",
                "measured" if text_box else "unavailable",
                "Probe slide PNG difference after removing text from a fixed-size frame",
            )
        )
    return replace(
        shape,
        visible_object_box=object_box,
        visible_text_box=text_box,
        measurements=(*shape.measurements, *notes),
    )
