from dataclasses import dataclass
from enum import StrEnum


class Purpose(StrEnum):
    PRESENTATION = "presentation"
    LECTURE = "lecture"
    REPORT = "report"


class Strength(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    STRONG = "strong"


PROPERTIES = frozenset({"font_size", "font_color", "font_family", "position", "size"})


@dataclass(frozen=True)
class AnalysisPolicy:
    purpose: Purpose = Purpose.PRESENTATION
    strength: Strength = Strength.STANDARD
    slides: tuple[int, ...] = ()
    locked_properties: frozenset[str] = frozenset()
    locked_shapes: frozenset[str] = frozenset()

    def __post_init__(self):
        if not isinstance(self.purpose, Purpose) or not isinstance(self.strength, Strength):
            raise ValueError("Use Purpose and Strength enum values")
        if self.locked_properties - PROPERTIES:
            raise ValueError("Unknown locked property")
        if any(type(n) is not int or n < 1 for n in self.slides):
            raise ValueError("Slide numbers must be positive integers")

    def allows_change(self, shape_key: str, property_name: str) -> bool:
        slide_number = int(shape_key.split(":")[0])
        return (
            property_name in PROPERTIES
            and property_name not in self.locked_properties
            and shape_key not in self.locked_shapes
            and (not self.slides or slide_number in self.slides)
        )
