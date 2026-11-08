"""Purpose-based size recommendations; point values are not WCAG requirements."""

import math

from slideguard.domain.models import DeckSnapshot, Finding, Severity, Shape
from slideguard.domain.policy import (
    BODY_PLACEHOLDERS,
    TITLE_BODY_RATIO,
    TITLE_PLACEHOLDERS,
    AnalysisPolicy,
    Purpose,
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
            for number, result in enumerate(_evaluate_shape(shape, policy), start=1):
                findings.append(self._finding(f"{self.id}:{shape.key}:{number}", shape, result))

        by_slide: dict[int, list[Shape]] = {}
        for shape in shapes:
            by_slide.setdefault(shape.slide_number, []).append(shape)
        for slide_number in sorted(by_slide):
            found = _check_hierarchy(by_slide[slide_number], deck.height_pt)
            if found is not None:
                anchor, result, inferred = found
                rule_id = f"{self.id}.hierarchy.inferred" if inferred else self.id
                findings.append(
                    self._finding(f"{self.id}:{slide_number}:hierarchy", anchor, result, rule_id=rule_id)
                )
        return findings

    def _finding(
        self, finding_id: str, shape: Shape, result: _Result, *, rule_id: str | None = None
    ) -> Finding:
        severity, message, measured, expected, evidence = result
        return Finding(
            id=finding_id,
            rule_id=rule_id or self.id,
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


def _evaluate_shape(shape: Shape, policy: AnalysisPolicy) -> list[_Result]:
    if not shape.supported:
        reason = "; ".join(shape.notes) or "지원하지 않는 객체 유형"
        return [_unknown("이 객체는 글자 크기를 판정할 수 없습니다", reason)]
    runs = [run for run in shape.runs if run.text.strip()]
    if not runs:
        return [_unknown("글자 크기 정보가 없어 판정할 수 없습니다", "텍스트 런(run) 정보 없음")]

    threshold = policy.font_limits
    label = _PURPOSE_LABEL[policy.purpose]
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


def _unmarked_boxes(shapes: list[Shape]) -> list[Shape]:
    return sorted(
        (
            shape
            for shape in shapes
            if shape.supported
            and shape.placeholder_type is None
            and shape.shape_type in (1, 17)
            and shape.box is not None
            and _has_text(shape)
        ),
        key=lambda shape: shape.box.y,
    )


def _infer_roles(
    titles: list[Shape], bodies: list[Shape], boxes: list[Shape], height: float
) -> tuple[list[Shape], list[Shape]]:
    if titles:
        anchor = titles[0]
        if anchor.box is None:
            return titles, bodies
    else:
        if not boxes or height <= 0:
            return titles, bodies
        anchor = boxes[0]
        text = anchor.text.strip()
        if not 0 <= anchor.box.y <= height * 0.25 or "\n" in text or len(text) > 100:
            return titles, bodies
        if len(boxes) > 1 and boxes[1].box.y < anchor.box.y + anchor.box.height:
            return titles, bodies
        if bodies and not any(
            shape.box and shape.box.y >= anchor.box.y + anchor.box.height for shape in bodies
        ):
            return titles, bodies
        titles = [anchor]
    if not bodies:
        bodies = [shape for shape in boxes if anchor.box.y + anchor.box.height <= shape.box.y < height * 0.85]
    return titles, bodies


def _check_hierarchy(shapes: list[Shape], height: float) -> tuple[Shape, _Result, bool] | None:
    """한 슬라이드의 제목과 본문 크기 차이를 본다.

    제목이나 본문이 없는 슬라이드(표지·전체 이미지 등)는 검사 대상이 아니라
    None을 돌려준다. 자리는 찾았는데 크기를 모르면 unknown으로 남긴다.
    """
    titles = _role_shapes(shapes, TITLE_PLACEHOLDERS)
    bodies = _role_shapes(shapes, BODY_PLACEHOLDERS)
    inferred = not titles or not bodies
    if inferred:
        titles, bodies = _infer_roles(titles, bodies, _unmarked_boxes(shapes), height)
    if not titles or not bodies:
        return None

    anchor = max(titles, key=lambda title: _largest_run_pt([title]) or 0)
    title_pt = _largest_run_pt([anchor])
    body_pt = _largest_run_pt(bodies)
    if title_pt is None or body_pt is None:
        missing = "제목" if title_pt is None else "본문"
        result = _unknown(
            "제목과 본문의 크기를 비교할 수 없습니다",
            f"{missing} 글자 크기가 지정되지 않음(테마 상속 등)",
        )
        return anchor, result, inferred

    minimum_pt = body_pt * TITLE_BODY_RATIO
    if title_pt >= minimum_pt or math.isclose(title_pt, minimum_pt, rel_tol=1e-9):
        return None
    sizes = f"제목 {_fmt(title_pt)} / 본문 {_fmt(body_pt)}"
    rule = f"본문의 {TITLE_BODY_RATIO}배인 {_fmt(minimum_pt)} 이상 필요 (시각적 위계 기준)"
    if inferred:
        rule += " · 텍스트 상자의 위치를 기준으로 제목 또는 본문을 추정"
    if title_pt <= body_pt and not inferred:
        return (
            anchor,
            (
                "warning",
                f"제목이 본문보다 크지 않습니다 ({_fmt(title_pt)})",
                title_pt,
                minimum_pt,
                f"{sizes} — 제목이 본문 이하라 위계가 없음; {rule}",
            ),
            inferred,
        )
    return (
        anchor,
        (
            "review",
            f"제목과 본문의 크기 차이가 작습니다 ({_fmt(title_pt)})",
            title_pt,
            minimum_pt,
            f"{sizes} — {rule}; 의도한 편집인지 확인",
        ),
        inferred,
    )
