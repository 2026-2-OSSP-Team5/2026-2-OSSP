"""Confirm a uniform background only where no layer composition needs to be inferred."""

from slideguard.domain.models import Box, Color

from .colors import read_color
from .properties import number, read_optional, tristate


def _opaque_fill(fill, label: str, notes: list[str]) -> Color | None:
    visible = tristate(
        read_optional(lambda: fill.Visible, f"{label} visibility", notes), f"{label} visibility", notes
    )
    fill_type = read_optional(lambda: fill.Type, f"{label} type", notes)
    transparency = number(
        read_optional(lambda: fill.Transparency, f"{label} transparency", notes),
        f"{label} transparency",
        notes,
        minimum=0,
    )
    if visible is not True or type(fill_type) is not int or fill_type != 1 or transparency != 0:
        notes.append(f"{label} is not a confirmed opaque solid fill; no composite color was inferred")
        return None
    color_format = read_optional(lambda: fill.ForeColor, f"{label} color", notes)
    return read_color(color_format, f"{label} color", notes)


def read_background(
    shape, slide, *, box: Box, rendered_box: Box | None, shape_type: int, shape_count: int, notes: list[str]
) -> Color | None:
    if rendered_box is None:
        notes.append("Background coverage cannot be confirmed without verified text bounds")
        return None
    # A frontmost opaque rectangle covers lower slide/master layers. Other cases
    # need a compositor; this adapter does not diagnose or report shape overlaps.
    front = read_optional(lambda: shape.ZOrderPosition, "Shape z-order", notes)
    if type(front) is not int or front != shape_count:
        notes.append("Background is unknown because higher slide objects may affect the text")
        return None
    geometry = read_optional(lambda: shape.AutoShapeType, "Shape geometry", notes) if shape_type != 17 else 1
    rectangle = type(geometry) is int and geometry == 1
    contained = (
        box.x <= rendered_box.x
        and box.y <= rendered_box.y
        and rendered_box.x + rendered_box.width <= box.x + box.width
        and rendered_box.y + rendered_box.height <= box.y + box.height
    )
    line_visible = tristate(
        read_optional(lambda: shape.Line.Visible, "Shape line visibility", notes),
        "Shape line visibility",
        notes,
    )
    if not rectangle or not contained or line_visible is not False:
        notes.append(
            "Uniform background coverage is unverified for nonrectangular geometry, text outside the shape or a visible/unknown line"
        )
        return None
    fill = read_optional(lambda: shape.Fill, "Shape fill", notes)
    visible = tristate(
        read_optional(lambda: fill.Visible, "Shape fill visibility", notes), "Shape fill visibility", notes
    )
    if visible is True:
        return _opaque_fill(fill, "Shape fill", notes)
    if visible is not False:
        return None
    if shape_count != 1:
        notes.append("Unfilled text may have other slide objects behind it; background is unknown")
        return None
    master = read_optional(lambda: slide.DisplayMasterShapes, "Master shape visibility", notes)
    following = read_optional(lambda: slide.FollowMasterBackground, "Background inheritance", notes)
    layout_count = read_optional(lambda: slide.CustomLayout.Shapes.Count, "Layout shape count", notes)
    if (
        type(master) is not int
        or master != 0
        or type(following) is not int
        or following != 0
        or type(layout_count) is not int
        or layout_count != 0
    ):
        notes.append("Inherited background or master/layout decorations are unverified")
        return None
    for name in ("DateAndTime", "Footer", "SlideNumber"):
        visibility = read_optional(
            lambda name=name: getattr(slide.HeadersFooters, name).Visible, f"{name} visibility", notes
        )
        if type(visibility) is not int or visibility != 0:
            notes.append("Header/footer decoration visibility is unverified")
            return None
    slide_fill = read_optional(lambda: slide.Background.Fill, "Slide background fill", notes)
    return _opaque_fill(slide_fill, "Slide background fill", notes)
