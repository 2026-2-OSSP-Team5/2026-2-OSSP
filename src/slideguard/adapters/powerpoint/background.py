"""Confirm a uniform background only where no layer composition needs to be inferred."""

from slideguard.domain.models import Box, Color

from .colors import read_color
from .properties import number, read_optional, tristate

_PLACEHOLDER = 14  # msoPlaceholder: 레이아웃·마스터의 자리 표시자는 슬라이드에 그려지지 않음


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


def _intersects(a: Box, b: Box) -> bool:
    return a.x < b.x + b.width and b.x < a.x + a.width and a.y < b.y + b.height and b.y < a.y + a.height


def _background_fill(slide, notes: list[str]):
    """슬라이드에 실제로 적용되는 배경 채우기: 슬라이드 → 레이아웃 → 마스터 순으로 따라감."""
    owners = (
        ("Slide", lambda: slide),
        ("Layout", lambda: slide.CustomLayout),
        ("Master", lambda: slide.Master),
    )
    for index, (label, get_owner) in enumerate(owners):
        owner = read_optional(get_owner, f"{label} object", notes)
        if owner is None:
            return None
        if index < len(owners) - 1:
            follows = tristate(
                read_optional(
                    lambda owner=owner: owner.FollowMasterBackground, f"{label} background inheritance", notes
                ),
                f"{label} background inheritance",
                notes,
            )
            if follows is None:
                return None
            if follows:
                continue  # 위 단계(레이아웃·마스터)의 배경을 그대로 씀
        return read_optional(lambda owner=owner: owner.Background.Fill, f"{label} background fill", notes)
    return None


def _decoration_may_cover(collection, bounds: Box, label: str, notes: list[str]) -> bool:
    """레이아웃·마스터 도형 중 글자 영역에 그려질 수 있는 장식이 있으면 True (모르면 True)."""
    count = read_optional(lambda: collection.Count, f"{label} shape count", notes)
    if type(count) is not int:
        return True
    for index in range(1, count + 1):
        item = read_optional(lambda index=index: collection.Item(index), f"{label} shape", notes)
        if item is None:
            return True
        kind = read_optional(lambda item=item: item.Type, f"{label} shape type", notes)
        if type(kind) is not int:
            return True
        if kind == _PLACEHOLDER:
            continue  # 자리 표시자는 슬라이드에 그려지지 않음 (슬라이드의 자리 표시자는 슬라이드 도형으로 따로 셈)
        visible = tristate(
            read_optional(lambda item=item: item.Visible, f"{label} shape visibility", notes),
            f"{label} shape visibility",
            notes,
        )
        if visible is False:
            continue
        if visible is None:
            return True
        values = [
            number(
                read_optional(lambda item=item, name=name: getattr(item, name), f"{label} {name}", notes),
                name,
                notes,
            )
            for name in ("Left", "Top", "Width", "Height", "Rotation")
        ]
        if any(value is None for value in values):
            return True
        left, top, width, height, rotation = values
        if rotation % 360 != 0:
            return True  # 회전한 도형은 실제 영역을 확인하지 않고 '덮을 수 있음'으로 봄
        effects = (
            ("Shadow", lambda item=item: item.Shadow.Visible),
            ("Glow", lambda item=item: item.Glow.Radius),
            ("SoftEdge", lambda item=item: item.SoftEdge.Radius),
        )
        for name, read in effects:
            value = read_optional(read, f"{label} {name}", notes)
            if type(value) not in (int, float) or value != 0:
                return True  # 효과는 상자 밖까지 번질 수 있음
        if _intersects(Box(left, top, width, height), bounds):
            return True
    return False


def effective_slide_background(slide, bounds: Box, notes: list[str]) -> Color | None:
    """글자 영역(bounds) 뒤에 실제로 보이는 슬라이드 배경색. 단색이고 장식이 없을 때만.

    배경을 따로 지정하지 않은 슬라이드도 레이아웃·마스터(테마)의 배경을 따라 읽습니다.
    레이아웃·마스터 장식(로고·띠 등)이 글자 영역에 그려질 수 있으면 None (추측하지 않음).
    """
    display_master = tristate(
        read_optional(lambda: slide.DisplayMasterShapes, "Master shape visibility", notes),
        "Master shape visibility",
        notes,
    )
    if display_master is None:
        return None
    if display_master:
        layout = read_optional(lambda: slide.CustomLayout, "Layout object", notes)
        if layout is None:
            return None
        if _decoration_may_cover(
            read_optional(lambda: layout.Shapes, "Layout shapes", notes), bounds, "Layout", notes
        ):
            notes.append("Layout decorations may be drawn behind the text; background is unknown")
            return None
        layout_master = tristate(
            read_optional(lambda: layout.DisplayMasterShapes, "Layout master visibility", notes),
            "Layout master visibility",
            notes,
        )
        if layout_master is None:
            return None
        if layout_master:
            master = read_optional(lambda: slide.Master, "Master object", notes)
            if master is None or _decoration_may_cover(
                read_optional(lambda: master.Shapes, "Master shapes", notes), bounds, "Master", notes
            ):
                notes.append("Master decorations may be drawn behind the text; background is unknown")
                return None
    fill = _background_fill(slide, notes)
    if fill is None:
        return None
    return _opaque_fill(fill, "Effective slide background fill", notes)


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
    # 바닥글·날짜·번호가 켜져 있으면 슬라이드 도형으로 있어야 하는데, 이 상자 하나뿐이면 앞뒤가 맞지 않음
    for name in ("DateAndTime", "Footer", "SlideNumber"):
        visibility = read_optional(
            lambda name=name: getattr(slide.HeadersFooters, name).Visible, f"{name} visibility", notes
        )
        if type(visibility) is not int or visibility != 0:
            notes.append("Header/footer decoration visibility is unverified")
            return None
    # 채우기 없는 글상자 = 투명 → 뒤에 보이는 슬라이드 배경 (레이아웃·마스터에서 상속한 배경 포함)
    return effective_slide_background(slide, rendered_box, notes)
