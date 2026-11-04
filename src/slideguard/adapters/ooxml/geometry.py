"""Transform saved DrawingML rectangles into slide-space quadrilaterals."""

from math import cos, radians, sin

from slideguard.domain.models import Box


def _pair(transform, name, first, second):
    nodes = transform.xpath(f"./*[local-name()='{name}']")
    if not nodes:
        raise ValueError("Missing transform component")
    return int(nodes[0].get(first, "")), int(nodes[0].get(second, ""))


def _transform(transform, points, *, group=False):
    x, y = _pair(transform, "off", "x", "y")
    w, h = _pair(transform, "ext", "cx", "cy")
    if group:
        cx, cy = _pair(transform, "chOff", "x", "y")
        cw, ch = _pair(transform, "chExt", "cx", "cy")
        if cw <= 0 or ch <= 0:
            raise ValueError("Degenerate child transform")
        points = tuple((x + (px - cx) * w / cw, y + (py - cy) * h / ch) for px, py in points)
    angle = radians(int(transform.get("rot", "0")) / 60000)
    cosine, sine = cos(angle), sin(angle)
    center_x, center_y = x + w / 2, y + h / 2
    flip_x = -1 if transform.get("flipH") in {"1", "true"} else 1
    flip_y = -1 if transform.get("flipV") in {"1", "true"} else 1
    offsets = (((px - center_x) * flip_x, (py - center_y) * flip_y) for px, py in points)
    return tuple(
        (center_x + dx * cosine - dy * sine, center_y + dx * sine + dy * cosine) for dx, dy in offsets
    )


def saved_geometry(shape):
    """Return an authored rectangular envelope, excluding effects and glyphs."""
    element = shape.element
    nodes = element.xpath("./p:spPr/a:xfrm | ./p:grpSpPr/a:xfrm | ./p:xfrm")
    if not nodes:
        return None, ()
    transform = nodes[0]
    try:
        x, y = _pair(transform, "off", "x", "y")
        w, h = _pair(transform, "ext", "cx", "cy")
        points = _transform(transform, ((x, y), (x + w, y), (x + w, y + h), (x, y + h)))
        for ancestor in element.iterancestors():
            if not ancestor.tag.endswith("}grpSp"):
                continue
            parents = ancestor.xpath("./p:grpSpPr/a:xfrm")
            if not parents:
                return None, ()
            points = _transform(parents[0], points, group=True)
    except (ValueError, OverflowError):
        return None, ()
    points = tuple((x / 12700, y / 12700) for x, y in points)
    xs, ys = zip(*points, strict=True)
    return Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)), points
