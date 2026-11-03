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

# --- 검사 기준값 -------------------------------------------------------------
# 명암비: WCAG 2.2 AA (1.4.3 Contrast Minimum)
CONTRAST_NORMAL = 4.5  # 일반 텍스트
CONTRAST_LARGE = 3.0  # 대형 텍스트
LARGE_TEXT_PT = 18.0  # 이 크기 이상이면 대형 텍스트
LARGE_BOLD_TEXT_PT = 14.0  # 굵은 글씨는 이 크기 이상이면 대형 텍스트

# 안전 여백: 슬라이드 각 방향 길이의 비율 (ITU 권고 기반)
SAFE_MARGIN_RATIO = 0.05


@dataclass(frozen=True)
class FontSizeThreshold:
    """Purpose-based size recommendation; point values are not WCAG requirements."""

    recommended_pt: float  # 이 값 미만이면 검토(review)
    minimum_pt: float  # 이 값 미만이면 경고(warning)


# 발표용 기준은 가독성 평가 기준표를 따른다(본문 18~24pt 권장, 14pt 미만 경고).
# TODO: 강의용·보고용은 근거 자료가 없어 임시값으로 둔다. 팀 합의 후 확정한다.
FONT_SIZE_THRESHOLDS: dict[Purpose, FontSizeThreshold] = {
    Purpose.PRESENTATION: FontSizeThreshold(recommended_pt=18.0, minimum_pt=14.0),
    Purpose.LECTURE: FontSizeThreshold(recommended_pt=18.0, minimum_pt=14.0),
    Purpose.REPORT: FontSizeThreshold(recommended_pt=12.0, minimum_pt=10.0),
}


def font_size_threshold(purpose: Purpose) -> FontSizeThreshold:
    return FONT_SIZE_THRESHOLDS[purpose]


# 도형 좌표를 비교할 때 허용하는 오차(pt). 반올림·렌더링 오차로 인한 오탐을 줄인다.
GEOMETRY_TOLERANCE_PT = 1.0


@dataclass(frozen=True)
class DensityThreshold:
    """Heuristic text-density limits per slide; exceeding them requires review."""

    max_chars: int  # 슬라이드 전체 글자 수(공백 제외)
    max_lines: int  # 슬라이드 전체 줄(문단) 수


# TODO: 정보 밀도는 기준으로 삼을 수치가 없어 임시값으로 둔다.
# 실제 슬라이드로 오탐 비율을 확인한 뒤 확정한다.
DENSITY_THRESHOLDS: dict[Purpose, DensityThreshold] = {
    Purpose.PRESENTATION: DensityThreshold(max_chars=300, max_lines=12),
    Purpose.LECTURE: DensityThreshold(max_chars=400, max_lines=15),
    Purpose.REPORT: DensityThreshold(max_chars=800, max_lines=30),
}


def density_threshold(purpose: Purpose) -> DensityThreshold:
    return DENSITY_THRESHOLDS[purpose]


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
