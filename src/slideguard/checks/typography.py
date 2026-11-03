"""Purpose-based size recommendations; point values are not WCAG requirements."""

from slideguard.domain.models import DeckSnapshot, Finding, Severity, Shape
from slideguard.domain.policy import AnalysisPolicy, Purpose, font_size_threshold

# (severity, message, measured, expected, evidence)
_Result = tuple[Severity, str, float | str | None, float | str | None, str]

_PURPOSE_LABEL = {
    Purpose.PRESENTATION: "발표용",
    Purpose.LECTURE: "강의용",
    Purpose.REPORT: "보고용",
}


def _fmt(size_pt: float) -> str:
    return f"{size_pt:g}pt"


class TypographyRule:
    id = "typography"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        findings: list[Finding] = []
        for shape in deck.shapes:
            if policy.slides and shape.slide_number not in policy.slides:
                continue
            if not _has_text(shape):
                continue
            for number, result in enumerate(_evaluate_shape(shape, policy.purpose), start=1):
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


def _evaluate_shape(shape: Shape, purpose: Purpose) -> list[_Result]:
    if not shape.supported:
        reason = "; ".join(shape.notes) or "지원하지 않는 객체 유형"
        return [_unknown("이 객체는 글자 크기를 판정할 수 없습니다", reason)]
    runs = [run for run in shape.runs if run.text.strip()]
    if not runs:
        return [_unknown("글자 크기 정보가 없어 판정할 수 없습니다", "텍스트 런(run) 정보 없음")]

    threshold = font_size_threshold(purpose)
    label = _PURPOSE_LABEL[purpose]
    results: list[_Result] = []
    unknown_reported = False
    seen_sizes: set[float] = set()

    for run in runs:
        size = run.font_size_pt
        if size is None:
            if not unknown_reported:
                unknown_reported = True
                results.append(
                    _unknown(
                        "글자 크기를 알 수 없어 판정할 수 없습니다", "글자 크기가 지정되지 않음(테마 상속 등)"
                    )
                )
            continue
        if size in seen_sizes:
            continue
        seen_sizes.add(size)

        if size < threshold.minimum_pt:
            results.append(
                (
                    "warning",
                    f"글자가 너무 작습니다 ({_fmt(size)})",
                    size,
                    threshold.minimum_pt,
                    f"{_fmt(size)} < {_fmt(threshold.minimum_pt)} ({label} 최소 기준)",
                )
            )
        elif size < threshold.recommended_pt:
            results.append(
                (
                    "review",
                    f"권장 크기보다 작습니다 ({_fmt(size)})",
                    size,
                    threshold.recommended_pt,
                    f"{_fmt(size)} < {_fmt(threshold.recommended_pt)} ({label} 권장 크기, 의도한 작은 글씨인지 확인)",
                )
            )
    return results
