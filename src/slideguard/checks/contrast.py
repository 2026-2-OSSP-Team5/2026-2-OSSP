"""Use known foreground/background colors; unresolved colors must remain unknown."""

from slideguard.domain.models import Color, DeckSnapshot, Finding, Severity, Shape, TextRun
from slideguard.domain.policy import (
    CONTRAST_LARGE,
    CONTRAST_NORMAL,
    LARGE_BOLD_TEXT_PT,
    LARGE_TEXT_PT,
    AnalysisPolicy,
)

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
        for shape in deck.shapes:
            if policy.slides and shape.slide_number not in policy.slides:
                continue
            if not _has_text(shape):
                continue
            for number, result in enumerate(_evaluate_shape(shape), start=1):
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
                        box=shape.rendered_box or shape.box,
                    )
                )
        return findings


def _has_text(shape: Shape) -> bool:
    return bool(shape.text.strip()) or any(run.text.strip() for run in shape.runs)


def _unknown(message: str, evidence: str) -> _Result:
    return ("unknown", message, None, None, evidence)


def _evaluate_shape(shape: Shape) -> list[_Result]:
    if not shape.supported:
        reason = "; ".join(shape.notes) or "지원하지 않는 객체 유형"
        return [_unknown("이 객체는 명암비를 판정할 수 없습니다", reason)]
    runs = [run for run in shape.runs if run.text.strip()]
    if not runs:
        return [_unknown("글자 색 정보가 없어 명암비를 판정할 수 없습니다", "텍스트 런(run) 정보 없음")]
    if shape.background is None:
        return [
            _unknown(
                "배경색을 알 수 없어 명암비를 판정할 수 없습니다",
                "배경이 이미지·그라데이션·투명이거나 색을 확인하지 못함",
            )
        ]

    results: list[_Result] = []
    seen: set[tuple] = set()
    for run in runs:
        key = (run.color, run.font_size_pt, run.bold)
        if key in seen:
            continue
        seen.add(key)
        result = _judge_run(run, shape.background)
        if result is not None:
            results.append(result)
    return results


def _judge_run(run: TextRun, background: Color) -> _Result | None:
    if run.color is None:
        return _unknown(
            "글자 색을 알 수 없어 명암비를 판정할 수 없습니다", "글자 색이 지정되지 않음(테마 상속 등)"
        )

    ratio = contrast_ratio(run.color, background)
    if ratio >= CONTRAST_NORMAL:
        return None  # 일반·대형 어느 기준으로도 통과

    large = large_text_state(run.font_size_pt, run.bold)
    colors = f"글자 {hex_color(run.color)} / 배경 {hex_color(background)}"

    if ratio >= CONTRAST_LARGE and large is None:
        return _unknown(
            "글자 크기·굵기를 알 수 없어 일반/대형 기준을 정할 수 없습니다",
            f"{colors} → {ratio:.2f}:1 (일반 {CONTRAST_NORMAL}:1 미만, 대형 {CONTRAST_LARGE}:1 이상)",
        )
    if ratio >= CONTRAST_LARGE and large:
        return None  # 대형 텍스트 기준 통과

    expected = CONTRAST_NORMAL if large is False else CONTRAST_LARGE
    kind = "일반" if large is False else "대형"
    return (
        "warning",
        f"글자와 배경의 명암비가 부족합니다 ({ratio:.2f}:1)",
        round(ratio, 2),
        expected,
        f"{colors} → {ratio:.2f}:1 < {expected}:1 ({kind} 텍스트, WCAG 2.2 AA)",
    )
