"""Purpose-based size recommendations; point values are not WCAG requirements."""

import math

from slideguard.domain.models import DeckSnapshot, Finding, Severity, Shape
from slideguard.domain.policy import (
    BODY_PLACEHOLDERS,
    TITLE_BODY_RATIO,
    TITLE_PLACEHOLDERS,
    AnalysisPolicy,
    Purpose,
    font_size_threshold,
)

from .scope import target_shapes

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
        shapes = target_shapes(deck, policy)
        findings: list[Finding] = []
        for shape in shapes:
            if not _has_text(shape):
                continue
            for number, result in enumerate(_evaluate_shape(shape, policy.purpose), start=1):
                findings.append(self._finding(f"{self.id}:{shape.key}:{number}", shape, result))

        by_slide: dict[int, list[Shape]] = {}
        for shape in shapes:
            by_slide.setdefault(shape.slide_number, []).append(shape)
        for slide_number in sorted(by_slide):
            found = _check_hierarchy(by_slide[slide_number])
            if found is not None:
                anchor, result = found
                findings.append(self._finding(f"{self.id}:{slide_number}:hierarchy", anchor, result))
        return findings

    def _finding(self, finding_id: str, shape: Shape, result: _Result) -> Finding:
        severity, message, measured, expected, evidence = result
        return Finding(
            id=finding_id,
            rule_id=self.id,
            shape_key=shape.key,
            severity=severity,
            message=message,
            measured=measured,
            expected=expected,
            evidence=evidence,
            box=shape.rendered_box or shape.box,
        )


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


# --- 시각적 위계 -----------------------------------------------------------


def _role_shapes(shapes: list[Shape], roles: frozenset[int]) -> list[Shape]:
    """자리 표시자 종류로 제목·본문을 고른다. 종류를 모르면(None) 제외한다."""
    return [
        shape for shape in shapes if shape.supported and shape.placeholder_type in roles and _has_text(shape)
    ]


def _largest_run_pt(shapes: list[Shape]) -> float | None:
    """제목과 경쟁하는 건 가장 큰 글자다. 하나도 크기를 모르면 None."""
    sizes = [
        run.font_size_pt
        for shape in shapes
        for run in shape.runs
        if run.text.strip() and run.font_size_pt is not None
    ]
    return max(sizes) if sizes else None


def _required_title_pt(body_pt: float) -> float:
    """본문의 정해진 배수. 0.5pt 단위로 올려 수정하기 쉬운 값으로 만든다."""
    return math.ceil(body_pt * TITLE_BODY_RATIO * 2) / 2


def _check_hierarchy(shapes: list[Shape]) -> tuple[Shape, _Result] | None:
    """한 슬라이드의 제목과 본문 크기 차이를 본다.

    제목이나 본문이 없는 슬라이드(표지·전체 이미지 등)는 검사 대상이 아니라
    None을 돌려준다. 자리는 찾았는데 크기를 모르면 unknown으로 남긴다.
    """
    titles = _role_shapes(shapes, TITLE_PLACEHOLDERS)
    bodies = _role_shapes(shapes, BODY_PLACEHOLDERS)
    if not titles or not bodies:
        return None

    anchor = titles[0]
    title_pt = _largest_run_pt(titles)
    body_pt = _largest_run_pt(bodies)
    if title_pt is None or body_pt is None:
        missing = "제목" if title_pt is None else "본문"
        return anchor, _unknown(
            "제목과 본문의 크기를 비교할 수 없습니다",
            f"{missing} 글자 크기가 지정되지 않음(테마 상속 등)",
        )

    required_pt = _required_title_pt(body_pt)
    if title_pt >= required_pt:
        return None

    sizes = f"제목 {_fmt(title_pt)} / 본문 {_fmt(body_pt)}"
    rule = f"본문의 {TITLE_BODY_RATIO}배인 {_fmt(required_pt)} 이상 필요 (시각적 위계 기준)"
    if title_pt <= body_pt:
        return anchor, (
            "warning",
            f"제목이 본문보다 크지 않습니다 ({_fmt(title_pt)})",
            title_pt,
            required_pt,
            f"{sizes} — 제목이 본문 이하라 위계가 없음; {rule}",
        )
    return anchor, (
        "review",
        f"제목과 본문의 크기 차이가 작습니다 ({_fmt(title_pt)})",
        title_pt,
        required_pt,
        f"{sizes} — {rule}; 의도한 편집인지 확인",
    )
