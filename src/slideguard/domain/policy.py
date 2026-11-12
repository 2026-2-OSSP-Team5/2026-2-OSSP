import json
import math
from dataclasses import asdict, dataclass, field
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

# 안전 여백: 슬라이드 각 방향 길이의 비율 (팀 평가 기준표)
SAFE_MARGIN_RATIO = 0.05

# 시각적 위계: 제목은 본문 글자 크기의 이 배수 이상이어야 한다 (팀 합의 2026-11-06).
# 제목이 본문보다 작거나 같으면 경고, 배수에 못 미치면 검토로 본다.
TITLE_BODY_RATIO = 1.25

# 제목·본문 자리 표시자 구분값은 PowerPoint PpPlaceholderType을 따른다.
# 1 제목 / 3 가운데 제목 / 5 세로 제목, 2 본문 / 6 세로 본문 / 7 콘텐츠.
TITLE_PLACEHOLDERS = frozenset({1, 3, 5})
BODY_PLACEHOLDERS = frozenset({2, 6, 7})


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


# 발표용 줄 수는 평가 기준표의 6x6 법칙(한 슬라이드 6줄 이내)을 따른다.
# TODO: 글자 수와 강의·보고용 줄 수는 근거 자료가 없어 임시값으로 둔다. 팀 합의 후 확정한다.
DENSITY_THRESHOLDS: dict[Purpose, DensityThreshold] = {
    Purpose.PRESENTATION: DensityThreshold(max_chars=300, max_lines=6),
    Purpose.LECTURE: DensityThreshold(max_chars=400, max_lines=15),
    Purpose.REPORT: DensityThreshold(max_chars=800, max_lines=30),
}


def density_threshold(purpose: Purpose) -> DensityThreshold:
    return DENSITY_THRESHOLDS[purpose]


@dataclass(frozen=True)
class ThresholdOverrides:
    minimum_font: float | None = None
    recommended_font: float | None = None
    max_chars: int | None = None
    max_lines: int | None = None
    contrast_normal: float | None = None
    contrast_large: float | None = None
    margin_percent: float | None = None

    def __post_init__(self):
        for value in (self.minimum_font, self.recommended_font):
            if value is not None and (not math.isfinite(value) or not 0 < value <= 400):
                raise ValueError("글자 크기는 0보다 크고 400pt 이하여야 합니다.")
        for value in (self.max_chars, self.max_lines):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("글자 수와 줄 수는 양의 정수여야 합니다.")
        for value in (self.contrast_normal, self.contrast_large):
            if value is not None and (not math.isfinite(value) or not 1 <= value <= 21):
                raise ValueError("명암비는 1~21 사이여야 합니다.")
        if self.margin_percent is not None and not 0 <= self.margin_percent < 50:
            raise ValueError("안전 여백은 0% 이상 50% 미만이어야 합니다.")


@dataclass(frozen=True)
class AnalysisPolicy:
    purpose: Purpose = Purpose.PRESENTATION
    strength: Strength = Strength.STANDARD
    slides: tuple[int, ...] = ()
    locked_properties: frozenset[str] = frozenset()
    locked_shapes: frozenset[str] = frozenset()
    thresholds: ThresholdOverrides = field(default_factory=ThresholdOverrides)

    def __post_init__(self):
        if not isinstance(self.purpose, Purpose) or not isinstance(self.strength, Strength):
            raise ValueError("Use Purpose and Strength enum values")
        if self.locked_properties - PROPERTIES:
            raise ValueError("Unknown locked property")
        if any(type(n) is not int or n < 1 for n in self.slides):
            raise ValueError("Slide numbers must be positive integers")

        if self.font_limits.minimum_pt > self.font_limits.recommended_pt:
            raise ValueError("최소 글자 크기는 권장 크기 이하여야 합니다.")
        if self.large_contrast > self.normal_contrast:
            raise ValueError("대형 글자 명암비는 일반 글자 명암비 이하여야 합니다.")

    @property
    def font_limits(self) -> FontSizeThreshold:
        defaults = font_size_threshold(self.purpose)
        return FontSizeThreshold(
            self.thresholds.recommended_font or defaults.recommended_pt,
            self.thresholds.minimum_font or defaults.minimum_pt,
        )

    @property
    def density_limits(self) -> DensityThreshold:
        defaults = density_threshold(self.purpose)
        return DensityThreshold(
            self.thresholds.max_chars or defaults.max_chars,
            self.thresholds.max_lines or defaults.max_lines,
        )

    @property
    def normal_contrast(self) -> float:
        return self.thresholds.contrast_normal or CONTRAST_NORMAL

    @property
    def large_contrast(self) -> float:
        return self.thresholds.contrast_large or CONTRAST_LARGE

    @property
    def margin_ratio(self) -> float:
        percent = self.thresholds.margin_percent
        return SAFE_MARGIN_RATIO if percent is None else percent / 100

    @property
    def cache_key(self) -> str:
        return json.dumps(
            {
                "revision": 1,
                "purpose": self.purpose,
                "strength": self.strength,
                "slides": sorted(set(self.slides)),
                "locked_properties": sorted(self.locked_properties),
                "locked_shapes": sorted(self.locked_shapes),
                "thresholds": asdict(self.thresholds),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def allows_change(self, shape_key: str, property_name: str) -> bool:
        slide_number = int(shape_key.split(":")[0])
        return (
            property_name in PROPERTIES
            and property_name not in self.locked_properties
            and shape_key not in self.locked_shapes
            and (not self.slides or slide_number in self.slides)
        )
