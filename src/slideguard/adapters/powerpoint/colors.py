"""Read RGB values resolved by PowerPoint, including resolvable scheme/theme colors."""

from slideguard.domain.models import Color

from .properties import read_optional


def read_color(color_format, label: str, notes: list[str]) -> Color | None:
    color_type = read_optional(lambda: color_format.Type, f"{label} type", notes)
    if type(color_type) is not int or color_type not in (1, 2):  # RGB, scheme
        notes.append(f"{label} is not a confirmed RGB/scheme color")
        return None
    rgb = read_optional(lambda: color_format.RGB, f"{label} RGB", notes)
    if type(rgb) is not int or not 0 <= rgb <= 0xFFFFFF:
        notes.append(f"{label} RGB is unresolved; no default color was used")
        return None
    return rgb & 0xFF, (rgb >> 8) & 0xFF, (rgb >> 16) & 0xFF
