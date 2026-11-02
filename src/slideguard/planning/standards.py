"""용도별 가독성 기준값과 그 근거.

모든 기준값은 Criterion(값, 근거, 종류)로 저장합니다.
  종류: "표준"      — 공개 표준 수치를 그대로 사용
        "계산"      — 표준 수치 + 시청 조건으로 계산
        "가정"      — 시청 조건 등 팀이 정한 가정값 (사용자 설정으로 바꿀 수 있음)
        "사용자 지정" — 사용자가 직접 바꾼 값

※ B(검사 엔진)도 같은 기준값을 써야 검사와 수정 후보가 어긋나지 않습니다.
   팀 합의 후 이 모듈을 domain/policy.py 쪽으로 옮겨 '한 곳'에서 관리하세요.

----------------------------------------------------------------------
근거 요약
----------------------------------------------------------------------
[1] WCAG 2.1 SC 1.4.3: 일반 글자 4.5:1, 큰 글자 3:1.
    큰 글자 = 18pt 또는 굵게 14pt (CSS 약 24px / 18.5px).
    명암비 식의 +0.05는 'Typical Viewing Flare'(화면 반사광) 값.
[2] CSS Values and Units: 참조 픽셀 = 팔 길이 거리에서 약 0.0213°의 시각.
    → WCAG 큰 글자 크기를 '눈에 보이는 각도'로 바꿔 슬라이드 크기로 환산.
[3] 글자 높이 시각: 최소 16분각, 최적 20분각 (MIL-PRF-87268D, 화면 가독성 지침).
[4] 4/6/8 규칙: 기본 시청(글을 읽는 수준)은 화면 높이의 6배 거리까지.
[5] ANSI/INFOCOMM 3M-2011 (현 AVIXA V201.01:2021) 투사 화면 시스템 명암비:
    기본 의사결정(교실·회의실) 15:1.
[6] SMPTE ST 2046-1: 자막 안전 영역(Safe Title Area) = 화면 가로·세로의 90%.
    방송 기준이지만, 화면 가장자리가 잘리거나 왜곡(키스톤)되는 투사 환경에 같은 여백을 적용.
[7] Ou & Luo (2006) 두 색 조화 모델 (Color Research & Application, doi:10.1002/col.20208).
    색 후보의 '조화 점수' 표시에 사용 (0보다 크면 조화롭다고 봄). color.py 참고.

투사 화면 명암비 계산 ([1] + [5]):
  투사 화면은 검은색도 완전히 검지 않습니다. 시스템 명암비가 S이면
  화면 밝기 = 원래 밝기 + 1/(S-1) 만큼 들뜹니다 (WCAG의 0.05 자리를 대신함).
  가장 불리한 경우(어두운 쪽 색이 완전 검정)에도 투사된 명암비가 목표 T 이상이 되려면
      파일 속 WCAG 명암비 ≥ 1 + (T - 1) × e / 0.05,   e = 1/(S-1)
  S=15, T=4.5 → 6.0:1 / T=3 → 3.9:1
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

ARCMIN_RAD = math.pi / (180 * 60)  # 1분각을 라디안으로
CSS_PX_DEG = 0.0213  # [2] 참조 픽셀의 시각(도)
WCAG_FLARE = 0.05  # [1]
DEFAULT_SLIDE_HEIGHT_PT = 540.0  # PowerPoint 기본 슬라이드 높이 (7.5인치)


@dataclass(frozen=True)
class Criterion:
    value: float
    basis: str
    kind: str  # "표준" / "계산" / "가정" / "사용자 지정"


@dataclass
class Standards:
    purpose: str
    label: str
    min_font_pt: Criterion
    large_text_pt: Criterion
    large_bold_text_pt: Criterion
    contrast_normal: Criterion
    contrast_large: Criterion
    min_line_spacing: Criterion
    safe_margin_ratio: Criterion
    max_lines: Criterion
    max_words_per_line: Criterion
    title_body_ratio: Criterion
    parameters: dict[str, Criterion] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def criteria(self) -> list[tuple[str, Criterion]]:
        """화면·보고서 출력용 (이름, 기준) 목록"""
        return [
            ("최소 글자 크기(pt)", self.min_font_pt),
            ("큰 글자 기준(pt)", self.large_text_pt),
            ("큰 글자 기준-굵게(pt)", self.large_bold_text_pt),
            ("명암비-일반 글자", self.contrast_normal),
            ("명암비-큰 글자", self.contrast_large),
            ("최소 줄 간격(배)", self.min_line_spacing),
            ("가장자리 여백(비율)", self.safe_margin_ratio),
            ("최대 줄 수", self.max_lines),
            ("줄당 최대 단어 수", self.max_words_per_line),
            ("제목/본문 크기 비", self.title_body_ratio),
        ]


# ======================================================================
# 용도별 프리셋 (시청 조건 가정)
#   viewing_ratio  : 가장 먼 사람의 거리 ÷ 화면에 보이는 슬라이드 높이
#   char_arcmin    : 글자 높이가 차지해야 하는 시각(분각)
#   system_contrast: 화면의 시스템 명암비 (None이면 일반 모니터 = WCAG 그대로)
# ======================================================================

PURPOSES = {
    "presentation": {
        "label": "발표 (회의실·프로젝터)",
        "viewing_ratio": (6.0, "4/6/8 규칙: 글을 읽는 기본 시청은 화면 높이의 6배 거리까지 [4]"),
        "char_arcmin": (16.0, "짧게 보는 발표 화면 → 최소 가독 글자 높이 16분각 [3]"),
        "system_contrast": (15.0, "교실·회의실 투사 화면 기본 의사결정 등급 15:1 [5]"),
    },
    "lecture": {
        "label": "강의 (강의실·장시간 필기)",
        "viewing_ratio": (6.0, "4/6/8 규칙: 강의실은 화면 높이의 6배 이내 권장 [4]"),
        "char_arcmin": (20.0, "오래 읽고 받아 적는 화면 → 최적 글자 높이 20분각 [3]"),
        "system_contrast": (15.0, "교실 투사 화면 기본 의사결정 등급 15:1 [5]"),
    },
    "online": {
        "label": "온라인 (화상회의 화면 공유)",
        "viewing_ratio": (
            5.0,
            "가정: 노트북과 눈 사이 약 60cm, 공유 창 안 슬라이드 높이 약 12cm (사용자 설정으로 조정)",
        ),
        "char_arcmin": (16.0, "최소 가독 글자 높이 16분각 [3]"),
        "system_contrast": (None, "일반 모니터 → WCAG 명암비 그대로 적용 [1]"),
    },
    "document": {
        "label": "배포 자료 (개인 화면·인쇄로 읽기)",
        "viewing_ratio": (
            2.0,
            "가정: 모니터 전체 화면 또는 A4 한 장 인쇄를 약 60cm/40cm에서 읽음 (사용자 설정으로 조정)",
        ),
        "char_arcmin": (20.0, "문서처럼 오래 읽음 → 최적 글자 높이 20분각 [3]"),
        "system_contrast": (None, "일반 모니터·인쇄물 → WCAG 명암비 그대로 적용 [1]"),
    },
}

DEFAULT_PURPOSE = "presentation"

GLYPH_HEIGHT_RATIO = (
    0.7,
    "가정: 글자 높이 ≈ 글꼴 크기의 0.7배 (라틴 대문자 기준, 한글은 이보다 커서 더 보수적)",
)
TARGET_NORMAL = (4.5, "WCAG 2.1 AA 일반 글자 [1]")
TARGET_LARGE = (3.0, "WCAG 2.1 AA 큰 글자 [1]")
MIN_LINE_SPACING = (1.0, "줄 간격이 1배 미만이면 윗줄과 아랫줄 글자가 겹칠 수 있음 (기하 조건)")
SAFE_MARGIN = (
    0.05,
    "SMPTE ST 2046-1 자막 안전 영역(가로·세로 90%) → 가장자리 5% 안쪽에 글자 배치 [6]",
    "표준",
)
MAX_LINES = (6, "가정: 6x6 법칙 (한 슬라이드 6줄 이내, 발표 자료 작성 경험칙 · 팀 결정)", "가정")
MAX_WORDS = (6, "가정: 6x6 법칙 (한 줄 6단어 이내, 발표 자료 작성 경험칙 · 팀 결정)", "가정")
TITLE_BODY_RATIO = (
    1.25,
    "가정: 제목이 본문보다 한 단계(1.25배) 이상 커야 위계가 구분됨 (타이포그래피 단계 비율 · 팀 결정)",
    "가정",
)

PARAMETER_KEYS = (
    "viewing_ratio",
    "char_arcmin",
    "system_contrast",
    "glyph_height_ratio",
    "target_contrast_normal",
    "target_contrast_large",
)
VALUE_KEYS = (
    "min_font_pt",
    "large_text_pt",
    "large_bold_text_pt",
    "contrast_normal",
    "contrast_large",
    "min_line_spacing",
    "safe_margin_ratio",
    "max_lines",
    "max_words_per_line",
    "title_body_ratio",
)


# ======================================================================
# 계산 함수
# ======================================================================


def _round_up(value: float, step: float) -> float:
    """기준을 느슨하게 만들지 않도록 항상 올림"""
    return round(math.ceil(value / step - 1e-9) * step, 2)


def min_font_for(viewing_ratio: float, char_arcmin: float, glyph_ratio: float, slide_height_pt: float) -> float:
    """글자 높이가 char_arcmin 이상 보이게 하는 최소 글꼴 크기(pt).

    화면 위 글자 높이 / 거리 = (글꼴 × glyph_ratio / 슬라이드 높이) × (화면 높이 / 거리)
                              = 글꼴 × glyph_ratio / (슬라이드 높이 × viewing_ratio)
    """
    angle = char_arcmin * ARCMIN_RAD
    return angle * slide_height_pt * viewing_ratio / glyph_ratio


def wcag_large_for(wcag_pt: float, viewing_ratio: float, slide_height_pt: float) -> float:
    """WCAG 큰 글자(18pt/14pt)가 데스크톱에서 보이는 각도와 같아지는 슬라이드 글꼴 크기."""
    css_px = wcag_pt * 4 / 3
    angle = math.radians(css_px * CSS_PX_DEG)
    return angle * slide_height_pt * viewing_ratio


def projected_contrast_requirement(target: float, system_contrast: float | None) -> float:
    """투사 화면에서도 target 이상 보이려면 파일에서 필요한 WCAG 명암비."""
    if system_contrast is None:
        return target
    e = 1 / (system_contrast - 1)
    if e <= WCAG_FLARE:  # 화면이 WCAG 가정보다 좋으면 그대로
        return target
    return max(target, 1 + (target - 1) * e / WCAG_FLARE)


# ======================================================================
# 기준 만들기
# ======================================================================


def build_standards(
    purpose: str = DEFAULT_PURPOSE,
    overrides: dict | None = None,
    slide_height_pt: float = DEFAULT_SLIDE_HEIGHT_PT,
) -> Standards:
    """용도 프리셋 + 사용자 설정으로 기준을 만듭니다.

    overrides에는 두 종류를 넣을 수 있습니다.
      - 시청 조건(PARAMETER_KEYS): 값을 바꾸면 기준값이 다시 계산됨
      - 기준값(VALUE_KEYS): 계산 결과를 직접 덮어씀
    """
    if purpose not in PURPOSES:
        raise ValueError(f"알 수 없는 용도: {purpose!r} (가능: {', '.join(PURPOSES)})")
    overrides = dict(overrides or {})
    for key in overrides:
        if key not in PARAMETER_KEYS and key not in VALUE_KEYS:
            raise ValueError(f"알 수 없는 설정 항목: {key!r}")

    preset = PURPOSES[purpose]

    # ---- 1. 시청 조건 (프리셋 → 사용자 설정) --------------------------
    params: dict[str, Criterion] = {}
    defaults = {
        "viewing_ratio": preset["viewing_ratio"],
        "char_arcmin": preset["char_arcmin"],
        "system_contrast": preset["system_contrast"],
        "glyph_height_ratio": GLYPH_HEIGHT_RATIO,
        "target_contrast_normal": TARGET_NORMAL,
        "target_contrast_large": TARGET_LARGE,
    }
    for key, (value, basis) in defaults.items():
        if key in overrides:
            params[key] = Criterion(_check_param(key, overrides[key]), f"사용자 지정 (프리셋: {value})", "사용자 지정")
        else:
            kind = "가정" if basis.startswith("가정") else "표준"
            params[key] = Criterion(value, basis, kind)

    k = params["viewing_ratio"].value
    arcmin = params["char_arcmin"].value
    glyph = params["glyph_height_ratio"].value
    system = params["system_contrast"].value
    t_normal = params["target_contrast_normal"].value
    t_large = params["target_contrast_large"].value

    # ---- 2. 계산 -----------------------------------------------------
    calc_note = f"(거리비 {k}, 글자 시각 {arcmin}분각, 슬라이드 높이 {slide_height_pt:g}pt)"
    min_font = Criterion(
        _round_up(min_font_for(k, arcmin, glyph, slide_height_pt), 0.5),
        f"글자 높이 {arcmin}분각 이상이 되는 크기 {calc_note} [3][4]",
        "계산",
    )
    large = Criterion(
        _round_up(wcag_large_for(18, k, slide_height_pt), 0.5),
        f"WCAG 큰 글자 18pt와 같은 시각 크기로 환산 (거리비 {k}) [1][2]",
        "계산",
    )
    large_bold = Criterion(
        _round_up(wcag_large_for(14, k, slide_height_pt), 0.5),
        f"WCAG 큰 글자 굵게 14pt와 같은 시각 크기로 환산 (거리비 {k}) [1][2]",
        "계산",
    )
    if system is None:
        c_normal = Criterion(t_normal, "WCAG 2.1 AA 일반 글자 [1]", "표준")
        c_large = Criterion(t_large, "WCAG 2.1 AA 큰 글자 [1]", "표준")
    else:
        c_normal = Criterion(
            _round_up(projected_contrast_requirement(t_normal, system), 0.1),
            f"투사 화면(시스템 명암비 {system:g}:1)에서도 {t_normal}:1 이상 유지 [1][5]",
            "계산",
        )
        c_large = Criterion(
            _round_up(projected_contrast_requirement(t_large, system), 0.1),
            f"투사 화면(시스템 명암비 {system:g}:1)에서도 {t_large}:1 이상 유지 [1][5]",
            "계산",
        )
    spacing = Criterion(MIN_LINE_SPACING[0], MIN_LINE_SPACING[1], "표준")

    values = {
        "min_font_pt": min_font,
        "large_text_pt": large,
        "large_bold_text_pt": large_bold,
        "contrast_normal": c_normal,
        "contrast_large": c_large,
        "min_line_spacing": spacing,
        "safe_margin_ratio": Criterion(*SAFE_MARGIN),
        "max_lines": Criterion(*MAX_LINES),
        "max_words_per_line": Criterion(*MAX_WORDS),
        "title_body_ratio": Criterion(*TITLE_BODY_RATIO),
    }
    computed = dict(values)

    # ---- 3. 기준값 직접 덮어쓰기 ---------------------------------------
    for key in VALUE_KEYS:
        if key in overrides:
            new_value = _check_value(key, overrides[key])
            values[key] = Criterion(new_value, f"사용자 지정 (근거 계산값: {computed[key].value})", "사용자 지정")

    result = Standards(purpose=purpose, label=preset["label"], parameters=params, **values)
    result.warnings = _warnings(result, computed)
    return result


def load_overrides(path: str) -> dict:
    """사용자 설정 JSON 파일 읽기. 예: {"purpose": "lecture", "overrides": {"min_font_pt": 24}}"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError("설정 파일 최상위는 객체({ })여야 합니다.")
    return data


# ----------------------------------------------------------------------


def _check_param(key: str, value: object) -> float | None:
    if key == "system_contrast" and value is None:
        return None
    number = float(value)
    limits = {
        "viewing_ratio": (0.5, 20),
        "char_arcmin": (5, 60),
        "system_contrast": (2, 10000),
        "glyph_height_ratio": (0.3, 1.2),
        "target_contrast_normal": (1, 21),
        "target_contrast_large": (1, 21),
    }
    low, high = limits[key]
    if not low <= number <= high:
        raise ValueError(f"{key} 값 {number}이(가) 허용 범위 {low}~{high}를 벗어남")
    return number


def _check_value(key: str, value: object) -> float:
    number = float(value)
    limits = {
        "min_font_pt": (1, 200),
        "large_text_pt": (1, 200),
        "large_bold_text_pt": (1, 200),
        "contrast_normal": (1, 21),
        "contrast_large": (1, 21),
        "min_line_spacing": (0.5, 3),
        "safe_margin_ratio": (0, 0.25),
        "max_lines": (1, 50),
        "max_words_per_line": (1, 50),
        "title_body_ratio": (1, 4),
    }
    low, high = limits[key]
    if not low <= number <= high:
        raise ValueError(f"{key} 값 {number}이(가) 허용 범위 {low}~{high}를 벗어남")
    return number


def _warnings(s: Standards, computed: dict[str, Criterion]) -> list[str]:
    """사용자 설정이 근거보다 느슨해졌으면 경고 (막지는 않음, 보고서에 표시)"""
    result = []
    if s.contrast_normal.value < 4.5:
        result.append(f"일반 글자 명암비 {s.contrast_normal.value}:1은 WCAG AA(4.5:1)보다 낮습니다.")
    if s.contrast_large.value < 3.0:
        result.append(f"큰 글자 명암비 {s.contrast_large.value}:1은 WCAG AA(3:1)보다 낮습니다.")
    for key, name in (("min_font_pt", "최소 글자 크기"), ("min_line_spacing", "최소 줄 간격")):
        now = getattr(s, key)
        if now.kind == "사용자 지정" and now.value < computed[key].value:
            result.append(f"{name} {now.value}은(는) 근거 계산값 {computed[key].value}보다 작습니다.")
    for key, name in (("contrast_normal", "일반 글자 명암비"), ("contrast_large", "큰 글자 명암비")):
        now = getattr(s, key)
        if now.kind == "사용자 지정" and now.value < computed[key].value:
            result.append(f"{name} {now.value}:1은(는) 이 용도의 근거 계산값 {computed[key].value}:1보다 낮습니다.")
    return result
