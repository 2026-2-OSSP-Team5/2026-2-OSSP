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


class ContrastGoal(StrEnum):
    """명암비 수정 목표: 검사 기준(통과선)을 넘은 뒤 '얼마나 여유 있게' 고칠지.

    검사(경고 여부)는 항상 CONTRAST_NORMAL / CONTRAST_LARGE로 하고, 이 값은 수정 후보의 목표만 바꾼다.
    MINIMUM      기준선 바로 위 (원래 색에 가장 가까움, 2026-11 이전 동작)
    COMFORTABLE  큰 글자도 일반 글자 기준 4.5:1까지 (투사 환경의 대비 손실 여유)
    ENHANCED     WCAG 2.2 SC 1.4.6 (AAA): 일반 7:1, 큰 글자 4.5:1
    CUSTOM       FixTargets.contrast_ratio 에 적은 값
    """

    MINIMUM = "minimum"
    COMFORTABLE = "comfortable"
    ENHANCED = "enhanced"
    CUSTOM = "custom"


class FontGoal(StrEnum):
    """글자 크기 수정 목표. MINIMUM = 최소 크기(경고선)까지, RECOMMENDED = 권장 크기(검토선)까지."""

    MINIMUM = "minimum"
    RECOMMENDED = "recommended"


PROPERTIES = frozenset({"font_size", "font_color", "font_family", "position", "size"})

# --- 검사 기준값 -------------------------------------------------------------
# 명암비: WCAG 2.2 AA (1.4.3 Contrast Minimum)
CONTRAST_NORMAL = 4.5  # 일반 텍스트
CONTRAST_LARGE = 3.0  # 대형 텍스트
# 글자 크기 기준(발표용 14pt 등)은 PowerPoint 기본 슬라이드 높이(7.5인치 = 540pt) 기준이다.
# 화면에 띄우면 슬라이드 전체가 화면 크기에 맞춰 늘거나 줄기 때문에, 보이는 크기는 '글자 pt ÷ 슬라이드 높이'에 비례한다.
# 예) 높이 810pt(20×11.25인치) 슬라이드의 18pt = 기본 슬라이드의 12pt처럼 보인다.
# 기준은 더 큰 슬라이드에서만 그만큼 키운다 (작은 슬라이드에서 기준을 낮추지는 않음: 인쇄·모니터에서는 pt 그대로 보임).
REFERENCE_SLIDE_HEIGHT_PT = 540.0
MAX_SLIDE_SCALE = 2.0


def slide_scale(height_pt: float | None) -> float:
    """글자 크기 기준에 곱할 배수 (1 이상)."""
    if not height_pt or not math.isfinite(height_pt) or height_pt <= 0:
        return 1.0
    return min(MAX_SLIDE_SCALE, max(1.0, height_pt / REFERENCE_SLIDE_HEIGHT_PT))


LARGE_TEXT_PT = 18.0  # 이 크기 이상이면 대형 텍스트
LARGE_BOLD_TEXT_PT = 14.0  # 굵은 글씨는 이 크기 이상이면 대형 텍스트
# 명암비 향상 기준: WCAG 2.2 AAA (1.4.6 Contrast Enhanced) — 수정 목표(ContrastGoal.ENHANCED)에만 쓴다
CONTRAST_ENHANCED_NORMAL = 7.0
CONTRAST_ENHANCED_LARGE = 4.5

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
# 발표용 글자 수는 6x6의 "한 줄 6단어"를 한국어로 환산한 값이다. 같은 글꼴·같은
# 크기에서 영어 단어와 한국어 어절의 실제 자폭을 재어 환산했고(슬라이드 117장),
# 환산 과정과 측정값은 tests/test_policy_thresholds.py 에 적어 두었다.
# 글꼴에 따라 자폭이 달라지므로 기본값이며, 사용자가 ThresholdOverrides로 조정한다.
# TODO: 강의·보고용은 근거 자료도 팀 합의도 없어 발표용의 배수로 둔 제안값이다.
# 교수님 면담(2026-11-10)에서 용도별 기준은 연구가 없어 팀이 정할 수밖에 없다고
# 하셨다. 회의에서 확정한다.
DENSITY_THRESHOLDS: dict[Purpose, DensityThreshold] = {
    Purpose.PRESENTATION: DensityThreshold(max_chars=100, max_lines=6),
    Purpose.LECTURE: DensityThreshold(max_chars=150, max_lines=9),  # 발표용 x 1.5
    Purpose.REPORT: DensityThreshold(max_chars=300, max_lines=18),  # 발표용 x 3
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
class FixTargets:
    """권장 수정의 목표값 (검사 기준과 별개).

    수정 후보는 두 가지로 나온다.
      최소 수정  기준을 겨우 넘기는, 원래 모습에 가장 가까운 후보 (항상 만듦)
      권장 수정  이 목표까지 여유 있게 고친 후보 (최소 수정과 값이 다를 때만 만듦)
    두 목표를 모두 MINIMUM으로 두면 권장 수정은 만들지 않는다.
    """

    contrast_goal: ContrastGoal = ContrastGoal.COMFORTABLE
    contrast_ratio: float | None = None  # CUSTOM일 때 목표 명암비 (1~21)
    font_goal: FontGoal = FontGoal.RECOMMENDED
    # TODO(B 검사 규칙 추가 후): 줄 간격 최소 배수. None이면 줄 간격 후보를 만들지 않음
    line_spacing: float | None = None
    # TODO(C): 같은 슬라이드의 사진 위 배경판을 한 가지 색·투명도로 통일
    unify_scrims: bool = False

    def __post_init__(self):
        # 설정 파일에서 문자열로 읽어 와도 enum으로 바꾼다
        object.__setattr__(self, "contrast_goal", ContrastGoal(self.contrast_goal))
        object.__setattr__(self, "font_goal", FontGoal(self.font_goal))
        if self.contrast_goal == ContrastGoal.CUSTOM:
            if self.contrast_ratio is None:
                raise ValueError("직접 지정한 명암비 목표가 없습니다.")
        if self.contrast_ratio is not None and (
            not math.isfinite(self.contrast_ratio) or not 1 <= self.contrast_ratio <= 21
        ):
            raise ValueError("명암비 목표는 1~21 사이여야 합니다.")
        if self.line_spacing is not None and (
            not math.isfinite(self.line_spacing) or not 1 <= self.line_spacing <= 3
        ):
            raise ValueError("줄 간격 목표는 1~3배 사이여야 합니다.")

    @classmethod
    def minimum(cls) -> "FixTargets":
        """최소 수정용 목표: 검사 기준을 겨우 넘기는 값."""
        return cls(ContrastGoal.MINIMUM, None, FontGoal.MINIMUM)

    @property
    def offers_recommended(self) -> bool:
        return self.contrast_goal != ContrastGoal.MINIMUM or self.font_goal != FontGoal.MINIMUM

    def contrast_target(self, required: float, *, large: bool, normal: float = CONTRAST_NORMAL) -> float:
        """검사 기준 required(그 글자에 적용된 통과선)를 넘는 수정 목표 명암비."""
        if self.contrast_goal == ContrastGoal.COMFORTABLE:
            goal = normal
        elif self.contrast_goal == ContrastGoal.ENHANCED:
            goal = CONTRAST_ENHANCED_LARGE if large else CONTRAST_ENHANCED_NORMAL
        elif self.contrast_goal == ContrastGoal.CUSTOM:
            goal = float(self.contrast_ratio)
        else:
            goal = required
        return max(required, goal)  # 어떤 목표도 검사 기준보다 낮아지지 않음


@dataclass(frozen=True)
class AnalysisPolicy:
    purpose: Purpose = Purpose.PRESENTATION
    strength: Strength = Strength.STANDARD
    slides: tuple[int, ...] = ()
    locked_properties: frozenset[str] = frozenset()
    locked_shapes: frozenset[str] = frozenset()
    thresholds: ThresholdOverrides = field(default_factory=ThresholdOverrides)
    fix: FixTargets = field(default_factory=FixTargets)  # 수정 후보의 목표 (검사에는 영향 없음)

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
                "revision": 4,
                "purpose": self.purpose,
                "strength": self.strength,
                "slides": sorted(set(self.slides)),
                "locked_properties": sorted(self.locked_properties),
                "locked_shapes": sorted(self.locked_shapes),
                "thresholds": asdict(self.thresholds),
                "fix": asdict(self.fix),
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
