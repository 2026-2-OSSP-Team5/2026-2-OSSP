"""Read geometry and paint facts without applying readability policy."""

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
        if result.box is not None and result.rendered_box is not None and None not in margins:
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
        properties=frame_details,
    )


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
