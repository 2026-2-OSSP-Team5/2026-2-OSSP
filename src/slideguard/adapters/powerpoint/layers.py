"""Resolve uniform color only for covered, verified rectangular layers."""

from dataclasses import replace

from .background import _opaque_fill
from .measurements import optional


def _intersects(a, b):
    return a.x < b.x + b.width and b.x < a.x + a.width and a.y < b.y + b.height and b.y < a.y + a.height


def _contains(a, b):
    return a.x <= b.x and a.y <= b.y and a.x + a.width >= b.x + b.width and a.y + a.height >= b.y + b.height


def _safe_rectangle(item):
    for name, expected in (("AutoShapeType", 1), ("Rotation", 0), ("HorizontalFlip", 0), ("VerticalFlip", 0)):
        if optional(lambda name=name: getattr(item, name), name, []) != expected:
            return False
    return (
        optional(lambda: item.Line.Visible, "Line visibility", []) == 0
        and optional(lambda: item.ThreeD.Visible, "3D visibility", []) == 0
        and optional(lambda: item.Shadow.Visible, "Shadow visibility", []) == 0
        and optional(lambda: item.Glow.Radius, "Glow radius", []) == 0
        and optional(lambda: item.SoftEdge.Radius, "Soft edge radius", []) == 0
    )


def _uniform(target, layers, slide):
    bounds = target.rendered_box
    if bounds is None or target.box is None or target.z_order is None or target.effective_visible is not True:
        return None
    # Even a nonintersecting bounding box may have an effect outside it.
    # Limit this extension to the case where every contributing shape is a
    # verified plain rectangle.
    for shape, item in layers:
        if shape.effective_visible is False:
            continue
        if shape.box is None or shape.z_order is None or not _safe_rectangle(item):
            return None
        if not _intersects(shape.box, bounds):
            continue
        if shape.z_order > target.z_order:
            return None
        fill = optional(lambda item=item: item.Fill, "Fill", [])
        visible = optional(lambda fill=fill: fill.Visible, "Fill visibility", [])
        if visible == 0:
            continue
        if not _contains(shape.box, bounds):
            return None
        return shape.fill_color if shape.fill_transparency == 0 else None
    if (
        optional(lambda: slide.DisplayMasterShapes, "Master visibility", []) != 0
        or optional(lambda: slide.CustomLayout.Shapes.Count, "Layout count", []) != 0
    ):
        return None
    if any(
        optional(lambda name=name: getattr(slide.HeadersFooters, name).Visible, name, []) != 0
        for name in ("DateAndTime", "Footer", "SlideNumber")
    ):
        return None
    fill = optional(lambda: slide.Background.Fill, "Effective slide fill", [])
    return _opaque_fill(fill, "Effective slide fill", []) if fill is not None else None


def resolve_backgrounds(shapes, slide):
    top = [shape for shape in shapes if shape.locator.kind == "shape" and len(shape.locator.shape_path) == 1]
    if len(top) <= 1:
        return shapes
    if any(shape.z_order is None for shape in top):
        return shapes
    layers = sorted(
        zip(top, (slide.Shapes.Item(i) for i in range(1, len(top) + 1)), strict=True),
        key=lambda pair: pair[0].z_order,
        reverse=True,
    )
    resolved = {}
    for shape in top:
        if shape.text and shape.background is None:
            color = _uniform(shape, layers, slide)
            if color is not None:
                resolved[shape.key] = replace(shape, background=color)
    return [resolved.get(shape.key, shape) for shape in shapes]
