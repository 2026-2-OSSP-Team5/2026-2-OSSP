"""Resolve uniform color only for covered, verified rectangular layers."""

import math
from dataclasses import replace

from slideguard.domain.models import Box

from .background import effective_slide_background
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


def _reach(shape, item):
    """이 도형이 그려질 수 있는 범위. 회전하면 돌아간 범위, 번지는 효과가 있거나 모르면 None."""
    for name, read in (
        ("Shadow visibility", lambda: item.Shadow.Visible),
        ("Glow radius", lambda: item.Glow.Radius),
        ("Soft edge radius", lambda: item.SoftEdge.Radius),
    ):
        if optional(read, name, []) != 0:
            return None  # 효과는 상자 밖까지 번질 수 있음 (얼마나 번지는지는 추측하지 않음)
    rotation = optional(lambda: item.Rotation, "Rotation", [])
    if type(rotation) not in (int, float):
        return None
    box = shape.box
    if rotation % 180 == 0:
        return box
    angle = math.radians(rotation % 180)
    cos, sin = abs(math.cos(angle)), abs(math.sin(angle))
    width = box.width * cos + box.height * sin
    height = box.width * sin + box.height * cos
    cx, cy = box.x + box.width / 2, box.y + box.height / 2
    return Box(cx - width / 2, cy - height / 2, width, height)


_TRANSPARENT_KINDS = (1, 17)  # msoAutoShape, msoTextBox
_PLACEHOLDER = 14


def _transparent(item):
    """채우기·선·3D가 없는 도형/글상자: 자기 글자 말고는 아무것도 그리지 않아 뒤 배경을 가리지 않음."""
    kind = optional(lambda: item.Type, "Object kind", [])
    if kind == _PLACEHOLDER:
        kind = optional(lambda: item.PlaceholderFormat.ContainedType, "Placeholder content", [])
    if kind not in _TRANSPARENT_KINDS:
        return False  # 그림·차트·표·그룹 등은 무엇을 그리는지 확인하지 않음
    return (
        optional(lambda: item.Fill.Visible, "Fill visibility", []) == 0
        and optional(lambda: item.Line.Visible, "Line visibility", []) == 0
        and optional(lambda: item.ThreeD.Visible, "3D visibility", []) == 0
    )


def _uniform(target, layers, slide, notes):
    """글자 영역 뒤에 보이는 단색 배경. 정할 수 없으면 None과 함께 notes에 이유를 남김."""
    bounds = target.rendered_box
    if bounds is None or target.box is None or target.z_order is None or target.effective_visible is not True:
        notes.append("Background is unknown: text bounds, drawing order or visibility is unverified")
        return None
    for shape, item in layers:
        if shape.effective_visible is False:
            continue
        if shape.box is None or shape.z_order is None:
            notes.append(f"Background is unknown: object {shape.shape_id} has no verified geometry or order")
            return None
        # 글자 영역에 닿지 않는 도형(구석의 사진·아이콘 등)은 배경에 영향을 주지 않음.
        # 회전·효과 때문에 닿는지 확인할 수 없는 도형만 보수적으로 '모름'.
        reach = _reach(shape, item)
        if reach is None:
            notes.append(
                f"Background is unknown: object {shape.shape_id} has a shadow/glow/soft edge or unknown rotation"
            )
            return None
        if not _intersects(reach, bounds):
            continue
        if shape.z_order > target.z_order:
            # 글자 위에 그려지는 도형: 투명하면(채우기·선 없음) 배경을 바꾸지 않음
            if shape.shape_id != target.shape_id and _transparent(item):
                continue
            notes.append(f"Background is unknown: object {shape.shape_id} is drawn over the text")
            return None
        if not _safe_rectangle(item):
            if shape.shape_id != target.shape_id and _transparent(item):
                continue  # 글자 뒤의 투명한 글상자: 배경을 바꾸지 않음
            notes.append(
                f"Background is unknown: object {shape.shape_id} under the text is not a plain rectangle"
            )
            return None
        fill = optional(lambda item=item: item.Fill, "Fill", [])
        visible = optional(lambda fill=fill: fill.Visible, "Fill visibility", [])
        if visible == 0:
            continue
        if not _contains(shape.box, bounds):
            notes.append(f"Background is unknown: filled object {shape.shape_id} only partly covers the text")
            return None
        if shape.fill_transparency != 0:
            notes.append(f"Background is unknown: object {shape.shape_id} under the text is semi-transparent")
            return None
        return shape.fill_color
    # 덮는 슬라이드 도형이 없으면 슬라이드 배경 (레이아웃·마스터에서 상속한 배경 포함)
    return effective_slide_background(slide, bounds, notes)


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
            notes: list[str] = []
            color = _uniform(shape, layers, slide, notes)
            if color is not None:
                resolved[shape.key] = replace(
                    shape, background=color, notes=(*shape.notes, "Background resolved from the slide layers")
                )
            elif notes:
                resolved[shape.key] = replace(shape, notes=(*shape.notes, notes[-1]))
    return [resolved.get(shape.key, shape) for shape in shapes]
