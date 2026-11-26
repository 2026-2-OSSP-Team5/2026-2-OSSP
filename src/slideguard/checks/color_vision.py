"""색약 맞춤 검사 (계획서 서브 기능 '색약 맞춤').

색약(색각 이상)이 있는 사람에게 슬라이드가 어떻게 보이는지 흉내 내어(시뮬레이션) 두 가지를 본다.

1) 명암비: 보통 눈에는 기준을 넘지만 색약 눈에는 기준 미달인 글자
   예) 검은 배경 위 빨간 글자는 적색약에게 훨씬 어둡게 보인다.
2) 색만으로 강조: 같은 상자 안에서 색만 바꿔 강조한 글자(굵기·기울임·밑줄·크기는 같음)가
   색약 눈에는 본문과 거의 같은 색으로 보이는 경우
   WCAG 2.2 SC 1.4.1 Use of Color: 색만으로 정보를 전달하지 말 것.

시뮬레이션: Machado, Oliveira & Fernandes (2009, IEEE TVCG 15(6)) 의 행렬, 심한 정도 1.0
            (적색약 protan, 녹색약 deutan, 청색약 tritan). 선형 RGB에 곱한다.
두 색의 구분: CIELAB 색차(ΔE76). 15 미만이면 '구분이 어렵다'로 본다 (팀 기준값, 비교용 근사).
모두 '검토'로 낸다: 실제 보이는 정도는 사람·화면마다 다르다.
"""

from __future__ import annotations

import math

from slideguard.domain.models import Color, DeckSnapshot, Finding, Shape
from slideguard.domain.policy import AnalysisPolicy

from .contrast import contrast_ratio, large_text_state
from .scope import target_shapes

# Machado et al. (2009) 심한 정도 1.0 행렬 (선형 RGB → 선형 RGB)
CVD_MATRICES: dict[str, tuple[tuple[float, float, float], ...]] = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}
CVD_LABELS = {"protan": "적색약", "deutan": "녹색약", "tritan": "청색약"}
DISTINCT_DELTA_E = 15.0  # 이보다 색차가 작으면 구분이 어렵다고 본다 (팀 기준값)


# ---------------------------------------------------------------- 색 계산


def _to_linear(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(value: float) -> int:
    value = min(1.0, max(0.0, value))
    c = value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return round(c * 255)


def simulate(color: Color, kind: str) -> Color:
    """색약 kind의 눈에 보이는 색 (근사)."""
    linear = [_to_linear(channel) for channel in color]
    matrix = CVD_MATRICES[kind]
    mixed = [sum(row[i] * linear[i] for i in range(3)) for row in matrix]
    return (_to_srgb(mixed[0]), _to_srgb(mixed[1]), _to_srgb(mixed[2]))


def worst_cvd_contrast(
    foreground: Color, background: Color, kinds: tuple[str, ...] | None = None
) -> tuple[float, str | None]:
    """보통 눈과 고른 색약 눈(kinds, 없으면 세 가지 모두) 가운데 가장 낮은 명암비와 그 눈 (보통 눈이면 None)."""
    worst, who = contrast_ratio(foreground, background), None
    for kind in CVD_MATRICES if kinds is None else kinds:
        ratio = contrast_ratio(simulate(foreground, kind), simulate(background, kind))
        if ratio < worst:
            worst, who = ratio, kind
    return worst, who


def _lab(color: Color) -> tuple[float, float, float]:
    r, g, b = (_to_linear(channel) for channel in color)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(first: Color, second: Color) -> float:
    """CIELAB 색차 ΔE76 (0 = 같은 색)."""
    return math.dist(_lab(first), _lab(second))


def _hex(color: Color) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


# ---------------------------------------------------------------- 검사


class ColorVisionRule:
    id = "color_vision"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        kinds = policy.vision_kinds  # 설정에서 고른 색각 유형만 본다 (예: 적록색약 → 적색약·녹색약)
        if not kinds:
            return []  # 기본값 '없음': 색약 검사 안 함
        findings: list[Finding] = []
        for shape in target_shapes(deck, policy):
            if not shape.supported or not shape.text.strip():
                continue
            findings.extend(self._contrast(shape, policy, kinds))
            findings.extend(self._color_only(shape, kinds))
        return findings

    def _contrast(self, shape: Shape, policy: AnalysisPolicy, kinds: tuple[str, ...]) -> list[Finding]:
        """보통 눈에는 통과, 색약 눈에는 미달인 글자색 (색마다 한 번)."""
        background = shape.background
        if background is None:
            return []  # 배경색을 모르면 판정하지 않음 (사진 위 글자는 기존 명암비 검사가 맡음)
        found, seen = [], set()
        for run in shape.runs:
            if not run.text.strip() or run.color is None or run.color in seen:
                continue
            seen.add(run.color)
            large = large_text_state(run.font_size_pt, run.bold)
            required = policy.large_contrast if large else policy.normal_contrast
            normal = contrast_ratio(run.color, background)
            if normal < required:
                continue  # 보통 눈에도 미달이면 기존 명암비 검사가 이미 알림
            worst, who = worst_cvd_contrast(run.color, background, kinds)
            if who is None or worst >= required:
                continue
            found.append(
                Finding(
                    id=f"{self.id}.contrast:{shape.key}:{_hex(run.color)[1:]}",
                    rule_id=f"{self.id}.contrast",
                    shape_key=shape.key,
                    severity="review",
                    message=f"{CVD_LABELS[who]}이면 글자가 배경과 잘 구분되지 않습니다 ({worst:.2f}:1)",
                    measured=round(worst, 2),
                    expected=required,
                    evidence=(
                        f"글자 {_hex(run.color)} / 배경 {_hex(background)}: 보통 눈 {normal:.2f}:1, "
                        f"{CVD_LABELS[who]} 시뮬레이션 {worst:.2f}:1 < {required}:1 "
                        "(Machado et al. 2009 시뮬레이션, 근사)"
                    ),
                    box=shape.text_bounds or shape.box,
                )
            )
        return found

    def _color_only(self, shape: Shape, kinds: tuple[str, ...]) -> list[Finding]:
        """색만 다르게 써서 구분한 글자들이 색약 눈에는 같은 색으로 보이는지.

        같은 상자 안의 글자색 두 가지씩 비교한다 (본문색과 강조색, 강조색끼리: 예 '폐지'는 빨강, '유지'는 초록).
        굵기·기울임·밑줄·크기 중 하나라도 다르면 색만으로 구분한 것이 아니므로 뺀다.
        """
        runs = [run for run in shape.runs if run.text.strip() and run.color is not None]
        by_color: dict[Color, list] = {}
        for run in runs:
            by_color.setdefault(run.color, []).append(run)
        colors = list(by_color)
        if len(colors) < 2:
            return []
        # 본문색 = 글자 수가 가장 많은 색. 수정(굵게)은 본문색이 아닌 쪽에만 한다
        body = max(colors, key=lambda color: sum(len(run.text.strip()) for run in by_color[color]))
        confusing = []  # (첫 색, 둘째 색, 색약 종류, 보통 눈 색차, 색약 눈 색차)
        for index, first in enumerate(colors):
            for second in colors[index + 1 :]:
                if any(_styled_differently(run, by_color[second]) for run in by_color[first]):
                    continue  # 색 말고도 다른 표시가 있음
                normal = delta_e(first, second)
                if normal < DISTINCT_DELTA_E:
                    continue  # 보통 눈에도 비슷한 색 (구분 의도가 아님)
                for kind in kinds:
                    seen_as = delta_e(simulate(first, kind), simulate(second, kind))
                    if seen_as < DISTINCT_DELTA_E:
                        confusing.append((first, second, kind, normal, seen_as))
                        break
        if not confusing:
            return []
        # 대표로 알릴 쌍: 본문색이 끼지 않은 쌍(강조색끼리, 예 '폐지' 빨강 / '유지' 초록)을 먼저
        confusing.sort(key=lambda item: (body in item[:2], item[4]))
        first, second, kind, normal, seen_as = confusing[0]
        marked = sorted({color for pair in confusing for color in pair[:2] if color != body})
        words = [_short(by_color[color][0].text) for color in (first, second)]
        return [
            Finding(
                # id 끝: 바꿀 색들(본문색 제외) + "_" + 본문색 → 수정 후보가 '본문과도 구분되는 색'을 고를 때 씀
                id=f"{self.id}.color_only:{shape.key}:"
                + "-".join(_hex(color)[1:] for color in marked)
                + f"_{_hex(body)[1:]}",
                rule_id=f"{self.id}.color_only",
                shape_key=shape.key,
                severity="review",
                message=f"{CVD_LABELS[kind]}이면 색으로 구분한 글자가 같은 색으로 보입니다",
                measured=round(seen_as, 1),
                expected=DISTINCT_DELTA_E,
                evidence=(
                    f'"{words[0]}" {_hex(first)} 와 "{words[1]}" {_hex(second)} 의 색차가 '
                    f"보통 눈 {normal:.0f} → {CVD_LABELS[kind]} {seen_as:.0f} (기준 {DISTINCT_DELTA_E:g}). "
                    "굵게·밑줄·기호(✕ ○) 등 색 외의 표시도 함께 쓰세요 "
                    "(Okabe & Ito CUD 원칙 2, WCAG 2.2 SC 1.4.1 색 사용)"
                ),
                box=shape.text_bounds or shape.box,
            )
        ]


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= 12 else text[:12] + "…"


def _styled_differently(run, body_runs) -> bool:
    """본문과 굵기·기울임·밑줄·크기 중 하나라도 다른지 (모르면 같다고 봄)."""
    for body in body_runs:
        for name in ("bold", "italic", "underline", "font_size_pt"):
            mine, theirs = getattr(run, name, None), getattr(body, name, None)
            if mine is not None and theirs is not None and mine != theirs:
                return True
    return False
