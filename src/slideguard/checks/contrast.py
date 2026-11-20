"""Use known foreground/background colors; unresolved colors must remain unknown.

배경색을 하나로 정할 수 없을 때(사진·그라데이션·반투명 등)는 렌더링에서 모은
글자 주변 픽셀(background_samples)이 있으면 '가장 불리한 픽셀' 기준으로 근사 판정합니다.
  - 모든 픽셀에서 기준 통과 → 통과
  - 가장 유리한 픽셀에서도 미달 → 문제(warning): 배경 어디서도 읽기 어려우므로 확정
  - 일부 위치만 미달 → 검토 필요(review): 근사값이라 경고로 단정하지 않음
  - 픽셀 근거도 없으면 → 판정 불가(unknown)
"""

from slideguard.domain.models import Color, DeckSnapshot, Finding, Severity, Shape, TextRun
from slideguard.domain.policy import LARGE_BOLD_TEXT_PT, LARGE_TEXT_PT, AnalysisPolicy

from .scope import target_shapes

# (severity, message, measured, expected, evidence)
_Result = tuple[Severity, str, float | str | None, float | str | None, str]


def relative_luminance(color: Color) -> float:
    """WCAG 상대 휘도 (0.0 = 검정, 1.0 = 흰색)."""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = color
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(foreground: Color, background: Color) -> float:
    """WCAG 명암비 (1.0 ~ 21.0). 인자 순서와 무관하다."""
    lighter, darker = sorted((relative_luminance(foreground), relative_luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def large_text_state(size_pt: float | None, bold: bool | None) -> bool | None:
    """대형 텍스트(18pt 이상, 또는 굵은 14pt 이상)면 True, 아니면 False, 알 수 없으면 None."""
    if size_pt is None:
        return None
    if size_pt >= LARGE_TEXT_PT:
        return True
    if size_pt < LARGE_BOLD_TEXT_PT:
        return False
    # 14pt 이상 18pt 미만: 굵기에 따라 달라진다.
    return None if bold is None else bold


def hex_color(color: Color) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


class ContrastRule:
    id = "contrast"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        findings: list[Finding] = []
        for shape in target_shapes(deck, policy):
            if not _has_text(shape):
                continue
            for number, result in enumerate(_evaluate_shape(shape, policy), start=1):
                severity, message, measured, expected, evidence = result
                findings.append(
                    Finding(
                        id=f"{self.id}:{shape.key}:{number}",
                        rule_id=self.id,
                        shape_key=shape.key,
                        severity=severity,
                        message=message,
                        measured=measured,
                        expected=expected,
                        evidence=evidence,
                        box=shape.text_bounds or shape.box,
                    )
                )
        return findings


def _has_text(shape: Shape) -> bool:
    return bool(shape.text.strip()) or any(run.text.strip() for run in shape.runs)


def _unknown(message: str, evidence: str) -> _Result:
    return ("unknown", message, None, None, evidence)


def _evaluate_shape(shape: Shape, policy: AnalysisPolicy) -> list[_Result]:
    if not shape.supported:
        reason = "; ".join(shape.notes) or "지원하지 않는 객체 유형"
        return [_unknown("이 객체는 명암비를 판정할 수 없습니다", reason)]
    runs = [run for run in shape.runs if run.text.strip()]
    if not runs:
        return [_unknown("글자 색 정보가 없어 명암비를 판정할 수 없습니다", "텍스트 런(run) 정보 없음")]
    if shape.background is None:
        samples = _sample_colors(shape)
        if samples:
            return _evaluate_with_samples(runs, samples, policy)
        reason = _background_reason(shape)
        return [
            _unknown(
                "배경색을 알 수 없어 명암비를 판정할 수 없습니다",
                "배경이 이미지·그라데이션·투명이거나 색을 확인하지 못함"
                + (f" (추출 근거: {reason})" if reason else ""),
            )
        ]

    results: list[_Result] = []
    seen: set[tuple] = set()
    for run in runs:
        key = (run.color, run.font_size_pt, run.bold)
        if key in seen:
            continue
        seen.add(key)
        result = _judge_run(run, shape.background, policy)
        if result is not None:
            results.append(result)
    return results


def _background_reason(shape: Shape) -> str:
    """A(스냅샷)가 배경을 정하지 못한 이유 중 가장 마지막 기록."""
    for note in reversed(shape.notes):
        if "ackground" in note or "fill" in note.lower():
            return note
    return ""


# 렌더링 픽셀 근거로 판정하려면 최소 이만큼의 픽셀이 필요 (A의 렌더링 코드와 같은 값)
MIN_BACKGROUND_SAMPLES = 4
# 주변 픽셀 중 이 비율만큼은 무시하고 판정한다. 글상자 둘레를 따라 뽑은 픽셀에는 옆 도형의 테두리 선,
# 옆 글자 획 같은 가는 선이 섞이는데, '가장 나쁜 1개'로 판정하면 그 선 때문에 1.1:1 같은 오판정이 난다.
# (자바게티 20쪽: 베이지 배경의 검은 글씨가 둥근 테두리 선 때문에 1.1:1로 판정됨)
SAMPLE_OUTLIER_SHARE = 0.2


def sample_contrast(color: Color, samples: list[Color]) -> tuple[float, Color]:
    """주변 픽셀 대비 명암비: 가장 나쁜 쪽에서 SAMPLE_OUTLIER_SHARE만큼 뺀 뒤 가장 나쁜 값 (근사).

    planner(bindings)도 이 함수로 어떤 글자색의 판정인지 찾는다.
    """
    ranked = sorted(samples, key=lambda sample: contrast_ratio(color, sample))
    pick = ranked[min(len(ranked) - 1, int(len(ranked) * SAMPLE_OUTLIER_SHARE))]
    return contrast_ratio(color, pick), pick


def best_sample_contrast(color: Color, samples: list[Color]) -> float:
    """가장 유리한 쪽 명암비 (좋은 쪽에서도 SAMPLE_OUTLIER_SHARE만큼은 뺀다: 옆 글자·선 같은 잡음).

    이 값조차 기준 미달이면 배경 어디에 놓여도 읽기 어렵다는 뜻이라 '근사'가 아니라 확정 문제다.
    (예: 흰 카드 위 연회색 글자 → 주변 픽셀이 전부 흰색이라 가장 유리한 값도 2.6:1)
    """
    ranked = sorted(samples, key=lambda sample: contrast_ratio(color, sample))
    pick = ranked[max(0, len(ranked) - 1 - int(len(ranked) * SAMPLE_OUTLIER_SHARE))]
    return contrast_ratio(color, pick)


def _sample_colors(shape: Shape) -> list[Color]:
    samples = getattr(shape, "background_samples", ()) or ()
    colors = []
    for sample in samples:
        if len(sample) != 5:
            continue
        _x, _y, r, g, b = sample
        if all(type(value) is int and 0 <= value <= 255 for value in (r, g, b)):
            colors.append((r, g, b))
    return colors if len(colors) >= MIN_BACKGROUND_SAMPLES else []


def _evaluate_with_samples(
    runs: list[TextRun], samples: list[Color], policy: AnalysisPolicy
) -> list[_Result]:
    results: list[_Result] = []
    seen: set[tuple] = set()
    for run in runs:
        key = (run.color, run.font_size_pt, run.bold)
        if key in seen:
            continue
        seen.add(key)
        if run.color is None:
            results.append(
                _unknown(
                    "글자 색을 알 수 없어 명암비를 판정할 수 없습니다",
                    "글자 색이 지정되지 않음(테마 상속 등)",
                )
            )
            continue
        worst, worst_color = sample_contrast(run.color, samples)
        large = large_text_state(run.font_size_pt, run.bold)
        # 글자 유형을 모르면 더 엄격한 일반 기준 (근사 판정이라 보수적으로)
        expected = policy.large_contrast if large else policy.normal_contrast
        if worst >= expected:
            continue
        kind = "대형" if large else "일반"
        best = best_sample_contrast(run.color, samples)
        if best < expected:
            # 가장 밝은(유리한) 배경 위치에서도 미달 → 배경이 고르든 아니든 확정 문제
            results.append(
                (
                    "warning",
                    f"글자와 배경의 명암비가 부족합니다 ({best:.2f}:1)",
                    round(best, 2),
                    expected,
                    f"글자 {hex_color(run.color)} / 렌더링된 주변 픽셀 {len(samples)}개 중 가장 유리한 쪽도 "
                    f"{best:.2f}:1 < {expected}:1 ({kind} 텍스트 기준, 배경 어디서도 미달)",
                )
            )
            continue
        results.append(
            (
                "review",
                f"배경이 고르지 않아 일부 위치에서 명암비가 부족할 수 있습니다 (최저 {worst:.2f}:1, 근사)",
                round(worst, 2),
                expected,
                f"글자 {hex_color(run.color)} / 렌더링된 주변 픽셀 {len(samples)}개 중 가는 선 등 "
                f"{SAMPLE_OUTLIER_SHARE:.0%}를 뺀 가장 불리한 "
                f"{hex_color(worst_color)} → {worst:.2f}:1 < {expected}:1 ({kind} 텍스트 기준, "
                "배경이 사진·그라데이션·반투명이라 근사값)",
            )
        )
    return results


def _judge_run(run: TextRun, background: Color, policy: AnalysisPolicy) -> _Result | None:
    if run.color is None:
        return _unknown(
            "글자 색을 알 수 없어 명암비를 판정할 수 없습니다", "글자 색이 지정되지 않음(테마 상속 등)"
        )

    ratio = contrast_ratio(run.color, background)
    normal, large_threshold = policy.normal_contrast, policy.large_contrast
    basis = (
        "사용자 기준"
        if policy.thresholds.contrast_normal is not None or policy.thresholds.contrast_large is not None
        else "WCAG 2.2 AA"
    )
    if ratio >= normal:
        return None  # 일반·대형 어느 기준으로도 통과

    large = large_text_state(run.font_size_pt, run.bold)
    colors = f"글자 {hex_color(run.color)} / 배경 {hex_color(background)}"

    if ratio >= large_threshold and large is None:
        return _unknown(
            "글자 크기·굵기를 알 수 없어 일반/대형 기준을 정할 수 없습니다",
            f"{colors} → {ratio:.2f}:1 (일반 {normal}:1 미만, 대형 {large_threshold}:1 이상)",
        )
    if ratio >= large_threshold and large:
        return None  # 대형 텍스트 기준 통과

    if large is None:
        # 글자 유형을 모르지만 두 기준 모두 미달이라 위반은 확정이다.
        # 어느 쪽으로 밝혀지든 통과하려면 일반 텍스트 기준을 넘겨야 한다.
        return (
            "warning",
            f"글자와 배경의 명암비가 부족합니다 ({ratio:.2f}:1)",
            round(ratio, 2),
            normal,
            f"{colors} → {ratio:.2f}:1 (글자 유형 미확정: 크기·굵기를 알 수 없음) "
            f"< 일반 {normal}:1, 대형 {large_threshold}:1 — 두 기준 모두 미달 ({basis})",
        )

    expected = normal if large is False else large_threshold
    kind = "일반" if large is False else "대형"
    return (
        "warning",
        f"글자와 배경의 명암비가 부족합니다 ({ratio:.2f}:1)",
        round(ratio, 2),
        expected,
        f"{colors} → {ratio:.2f}:1 < {expected}:1 ({kind} 텍스트, {basis})",
    )
