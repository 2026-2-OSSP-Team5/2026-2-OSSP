import json
import math
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Purpose(StrEnum):
    PRESENTATION = "presentation"
    LECTURE = "lecture"
    REPORT = "report"


class Audience(StrEnum):
    """청중 (계획서 서브 기능 '청중 맞춤').

    GENERAL   일반 성인 (대학생·직장인): 용도별 기준 그대로
    CHILDREN  초등학생: 글자를 더 크게, 한 장의 정보량을 더 적게
    OLDER     고령 청중: 글자를 더 크게
    """

    GENERAL = "general"
    CHILDREN = "children"
    OLDER = "older"


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


# --- 청중 맞춤 ---------------------------------------------------------------
# 글자 크기 기준에 곱하는 배수와 정보 밀도 기준에 곱하는 배수.
#   어린이: Hughes & Wilkins (2000, J. Research in Reading) — 5~7세는 성인용보다 큰 글자에서 더 빨리 읽음.
#           한 장의 정보량은 줄 수·글자 수를 0.7배로 (팀 제안값)
#   고령:   Legge & Bigelow (2011, J. Vision) — 나이가 들면 '빨리 읽기 시작하는 최소 크기(임계 글자 크기)'가 커짐
# 배수 자체는 연구가 제시한 값이 아니라 팀 제안값이다 (방향만 근거 있음). 설정에서 직접 기준을 바꾸면 그 값이 우선.
AUDIENCE_FONT_FACTOR = {Audience.GENERAL: 1.0, Audience.CHILDREN: 1.25, Audience.OLDER: 1.25}
AUDIENCE_DENSITY_FACTOR = {Audience.GENERAL: 1.0, Audience.CHILDREN: 0.7, Audience.OLDER: 1.0}
AUDIENCE_LABELS = {Audience.GENERAL: "일반", Audience.CHILDREN: "어린이", Audience.OLDER: "고령"}


# --- 환경 맞춤: 화면 크기와 맨 뒷줄 거리로 최소 글자 크기 계산 ---------------------
# 글자가 눈에 보이는 크기는 pt가 아니라 '보이는 각도(시각도)'로 정해진다.
#   Legge & Bigelow (2011, "Does print size matter for reading?", J. Vision 11(5):8):
#     정상 시력의 '임계 글자 크기'(이보다 작으면 읽는 속도가 떨어지는 크기)는 소문자 높이(x-height) 약 0.2°.
# 그래서 맨 뒷줄에서 x-height가 0.2° 미만이면 최소 기준(경고) 미달로 본다.
# 권장 기준(검토)은 용도별 기준의 '권장 ÷ 최소' 비율을 그대로 곱한다 (발표용 18/14 ≈ 1.29배).
# x-height는 글자 크기(em)의 약 0.5배로 본다 (라틴 글꼴 기준. 한글은 글자 높이가 더 커서 이 계산이 보수적).
CRITICAL_X_HEIGHT_DEG = 0.2
X_HEIGHT_PER_EM = 0.5
SCREEN_ASPECT = 16 / 9  # 화면 비율 (대부분의 프로젝터·TV)


@dataclass(frozen=True)
class ViewingSetup:
    """발표 환경. 둘 다 정해야 계산한다 (하나라도 None이면 쓰지 않음)."""

    screen_inches: float | None = None  # 화면 대각선 (인치)
    distance_m: float | None = None  # 화면에서 맨 뒷줄까지 거리 (m)

    def __post_init__(self):
        if self.screen_inches is not None and (
            not math.isfinite(self.screen_inches) or not 10 <= self.screen_inches <= 600
        ):
            raise ValueError("화면 크기는 10~600인치 사이여야 합니다.")
        if self.distance_m is not None and (
            not math.isfinite(self.distance_m) or not 0.3 <= self.distance_m <= 100
        ):
            raise ValueError("거리는 0.3~100m 사이여야 합니다.")

    @property
    def active(self) -> bool:
        return self.screen_inches is not None and self.distance_m is not None

    def minimum_pt(self, slide_width_pt: float, slide_height_pt: float, degrees: float) -> float | None:
        """맨 뒷줄에서 x-height가 degrees도로 보이려면 필요한 글자 크기 (슬라이드 pt 단위)."""
        if not self.active or slide_width_pt <= 0 or slide_height_pt <= 0:
            return None
        # 1) 화면의 실제 높이·너비 (m)
        diagonal_m = self.screen_inches * 0.0254
        screen_h = diagonal_m / math.sqrt(1 + SCREEN_ASPECT**2)
        screen_w = screen_h * SCREEN_ASPECT
        # 2) 슬라이드를 화면에 맞췄을 때 슬라이드 높이가 차지하는 실제 길이 (m)
        shown_h = min(screen_h, screen_w * slide_height_pt / slide_width_pt)
        # 3) 맨 뒷줄에서 그 각도로 보이는 x-height (m) → 글자 크기(em, m) → 슬라이드 pt
        x_height = 2 * self.distance_m * math.tan(math.radians(degrees) / 2)
        em = x_height / X_HEIGHT_PER_EM
        return round(em / shown_h * slide_height_pt, 1)

    def label(self) -> str:
        return f"{self.screen_inches:g}인치 화면, 맨 뒷줄 {self.distance_m:g}m"


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


# 색약 맞춤 검사에서 고를 수 있는 색각 유형 → 실제로 시뮬레이션할 눈
#   적록색약은 적색약(1형, protan)과 녹색약(2형, deutan)을 함께 부르는 말이라 둘 다 본다.
#   청색약(3형, tritan)은 드물지만 파랑·노랑 구분이 어렵다.
COLOR_VISION_KINDS: dict[str, tuple[str, ...]] = {
    "none": (),
    "red_green": ("protan", "deutan"),
    "protan": ("protan",),
    "deutan": ("deutan",),
    "tritan": ("tritan",),
    "all": ("protan", "deutan", "tritan"),
}


@dataclass(frozen=True)
class AnalysisPolicy:
    purpose: Purpose = Purpose.PRESENTATION
    strength: Strength = Strength.STANDARD
    slides: tuple[int, ...] = ()
    locked_properties: frozenset[str] = frozenset()
    locked_shapes: frozenset[str] = frozenset()
    thresholds: ThresholdOverrides = field(default_factory=ThresholdOverrides)
    fix: FixTargets = field(default_factory=FixTargets)  # 수정 후보의 목표 (검사에는 영향 없음)
    audience: Audience = Audience.GENERAL  # 청중 맞춤
    viewing: ViewingSetup = field(default_factory=ViewingSetup)  # 환경 맞춤 (화면 크기·거리)
    # 색약 맞춤 검사: 어떤 색각 이상을 고려할지 (COLOR_VISION_KINDS의 키). 기본은 "none" = 검사 안 함
    color_vision: str = "none"
    consistency: bool = True  # 슬라이드 간 디자인 일관성 검사
    image_text: bool = True  # 그림 속 글자 검사 (Windows 문자 인식)

    def __post_init__(self):
        if not isinstance(self.purpose, Purpose) or not isinstance(self.strength, Strength):
            raise ValueError("Use Purpose and Strength enum values")
        object.__setattr__(self, "audience", Audience(self.audience))  # 설정 파일의 문자열도 허용
        # 예전 설정은 켜기/끄기(True/False)였다: True는 세 가지 모두, False는 없음으로 바꿔 받는다
        if self.color_vision is True:
            object.__setattr__(self, "color_vision", "all")
        elif self.color_vision is False or self.color_vision is None:
            object.__setattr__(self, "color_vision", "none")
        if self.color_vision not in COLOR_VISION_KINDS:
            raise ValueError("알 수 없는 색각 유형입니다.")
        if self.locked_properties - PROPERTIES:
            raise ValueError("Unknown locked property")
        if any(type(n) is not int or n < 1 for n in self.slides):
            raise ValueError("Slide numbers must be positive integers")

        if self.font_limits.minimum_pt > self.font_limits.recommended_pt:
            raise ValueError("최소 글자 크기는 권장 크기 이하여야 합니다.")
        if self.large_contrast > self.normal_contrast:
            raise ValueError("대형 글자 명암비는 일반 글자 명암비 이하여야 합니다.")

    @property
    def vision_kinds(self) -> tuple[str, ...]:
        """검사할 색각 이상 종류 (protan=적색약, deutan=녹색약, tritan=청색약). 비어 있으면 색약 검사 안 함."""
        return COLOR_VISION_KINDS[self.color_vision]

    @property
    def font_limits(self) -> FontSizeThreshold:
        """용도별 기준 × 청중 배수 (직접 지정한 값이 있으면 그 값)."""
        defaults = font_size_threshold(self.purpose)
        factor = AUDIENCE_FONT_FACTOR[self.audience]
        return FontSizeThreshold(
            self.thresholds.recommended_font or round(defaults.recommended_pt * factor, 1),
            self.thresholds.minimum_font or round(defaults.minimum_pt * factor, 1),
        )

    def slide_font_limits(
        self, slide_width_pt: float | None, slide_height_pt: float | None
    ) -> tuple[FontSizeThreshold, str]:
        """이 덱에 실제로 쓰는 글자 크기 기준과 그 근거 설명.

        1) 용도·청중 기준 × 슬라이드 크기 배수 (큰 슬라이드는 같은 비율로 키움)
        2) 발표 환경(화면 크기·거리)을 정했으면 시각도로 계산한 값
        둘 중 큰 값을 쓴다 (환경 계산이 더 너그러워도 기본 기준 아래로는 내리지 않음).
        """
        base = self.font_limits
        scale = slide_scale(slide_height_pt)
        minimum = round(base.minimum_pt * scale, 1)
        recommended = round(base.recommended_pt * scale, 1)
        notes = []
        if scale > 1.0:
            notes.append(f"슬라이드가 기본보다 {scale:g}배 커서 기준도 ×{scale:g}")
        if self.audience != Audience.GENERAL and not self.thresholds.minimum_font:
            notes.append(f"청중 {AUDIENCE_LABELS[self.audience]} ×{AUDIENCE_FONT_FACTOR[self.audience]:g}")
        if self.viewing.active and slide_width_pt and slide_height_pt:
            env_min = self.viewing.minimum_pt(slide_width_pt, slide_height_pt, CRITICAL_X_HEIGHT_DEG)
            env_rec = None if env_min is None else round(env_min * base.recommended_pt / base.minimum_pt, 1)
            if env_min is not None and env_rec is not None and (env_min > minimum or env_rec > recommended):
                minimum, recommended = max(minimum, env_min), max(recommended, env_rec)
                notes.append(f"{self.viewing.label()}에서 보이는 크기 기준 (Legge & Bigelow 2011)")
        return FontSizeThreshold(recommended, minimum), ", ".join(notes)

    @property
    def density_limits(self) -> DensityThreshold:
        defaults = density_threshold(self.purpose)
        factor = AUDIENCE_DENSITY_FACTOR[self.audience]
        return DensityThreshold(
            self.thresholds.max_chars or max(1, round(defaults.max_chars * factor)),
            self.thresholds.max_lines or max(1, round(defaults.max_lines * factor)),
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
                "revision": 5,
                "purpose": self.purpose,
                "strength": self.strength,
                "slides": sorted(set(self.slides)),
                "locked_properties": sorted(self.locked_properties),
                "locked_shapes": sorted(self.locked_shapes),
                "thresholds": asdict(self.thresholds),
                "fix": asdict(self.fix),
                "audience": self.audience,
                "viewing": asdict(self.viewing),
                "color_vision": self.color_vision,
                "consistency": self.consistency,
                "image_text": self.image_text,
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
