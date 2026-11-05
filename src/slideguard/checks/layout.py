"""Overlap requires review and does not prove unintended placement."""

from collections import defaultdict
from itertools import combinations

from slideguard.domain.models import Box, DeckSnapshot, Finding, Severity, Shape
from slideguard.domain.policy import (
    GEOMETRY_TOLERANCE_PT,
    SAFE_MARGIN_RATIO,
    AnalysisPolicy,
    Purpose,
    density_threshold,
)

from .scope import target_shapes

TOL = GEOMETRY_TOLERANCE_PT


class LayoutRule:
    """여백·넘침·겹침·정보 밀도를 검사한다.

    Finding.rule_id 는 하위 검사를 구분하기 위해 ``layout.margin``, ``layout.overflow``,
    ``layout.overlap``, ``layout.density`` 를 사용한다.
    """

    id = "layout"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        shapes = target_shapes(deck, policy)
        findings: list[Finding] = []
        for shape in shapes:
            findings.extend(_check_margin(deck, shape))
            findings.extend(_check_overflow(shape))

        by_slide: dict[int, list[Shape]] = defaultdict(list)
        for shape in shapes:
            by_slide[shape.slide_number].append(shape)
        for slide_number in sorted(by_slide):
            slide_shapes = sorted(by_slide[slide_number], key=lambda s: s.shape_id)
            findings.extend(_check_overlap(slide_shapes))
            findings.extend(_check_density(slide_number, slide_shapes, policy.purpose))
        return findings


# --- 공통 도우미 ---------------------------------------------------------------
def _text_of(shape: Shape) -> str:
    return shape.text if shape.text.strip() else "".join(run.text for run in shape.runs)


def _has_text(shape: Shape) -> bool:
    return bool(_text_of(shape).strip())


def _effective_box(shape: Shape) -> Box | None:
    """렌더링 좌표를 알면 그것을, 모르면 도형 상자를 사용한다.

    상위 그룹의 회전·반전 등으로 슬라이드 좌표를 확인할 수 없으면 reader가 둘 다
    ``None``으로 남긴다. 그때는 좌표 기반 판정을 하지 않는다.
    """
    return shape.rendered_box or shape.box


def _finding(
    rule_id: str,
    suffix: str,
    shape: Shape,
    severity: Severity,
    message: str,
    measured: float | str | None,
    expected: float | str | None,
    evidence: str,
    box: Box | None = None,
) -> Finding:
    return Finding(
        id=f"{rule_id}:{suffix}",
        rule_id=rule_id,
        shape_key=shape.key,
        severity=severity,
        message=message,
        measured=measured,
        expected=expected,
        evidence=evidence,
        box=box or _effective_box(shape),
    )


def _geometry_unknown(rule_id: str, shape: Shape, label: str) -> Finding:
    reason = "; ".join(shape.notes) or "슬라이드 좌표를 확인할 수 없음"
    return _finding(rule_id, shape.key, shape, "unknown", f"{label}을 판정할 수 없습니다", None, None, reason)


# --- 안전 여백 -----------------------------------------------------------------
def _check_margin(deck: DeckSnapshot, shape: Shape) -> list[Finding]:
    if not _has_text(shape):
        return []
    box = _effective_box(shape)
    if box is None:
        return [_geometry_unknown("layout.margin", shape, "좌표를 확인할 수 없어 안전 여백")]
    margin_x = deck.width_pt * SAFE_MARGIN_RATIO
    margin_y = deck.height_pt * SAFE_MARGIN_RATIO
    sides = (
        ("왼쪽", box.x, margin_x),
        ("오른쪽", deck.width_pt - (box.x + box.width), margin_x),
        ("위", box.y, margin_y),
        ("아래", deck.height_pt - (box.y + box.height), margin_y),
    )
    violations = [(name, dist, need) for name, dist, need in sides if dist < need - TOL]
    if not violations:
        return []

    outside = [side for side in violations if side[1] < -TOL]
    detail = ", ".join(f"{name} {dist:.1f}pt(필요 {need:.1f}pt)" for name, dist, need in violations)
    if outside:
        # 슬라이드를 벗어난 변 중 가장 많이 나간 쪽을 보고한다.
        # 여백만 침범한 변을 고르면 방향과 수치가 실제 이탈과 어긋난다.
        severity: Severity = "warning"
        message = "텍스트가 슬라이드 밖으로 나갑니다"
        worst_name, worst_dist, worst_need = min(outside, key=lambda v: v[1])
    else:
        severity = "review"
        message = "텍스트가 안전 여백(5%) 안쪽에 있지 않습니다"
        worst_name, worst_dist, worst_need = min(violations, key=lambda v: v[1] - v[2])
    return [
        _finding(
            "layout.margin",
            shape.key,
            shape,
            severity,
            f"{message} ({worst_name})",
            round(worst_dist, 1),
            round(worst_need, 1),
            f"슬라이드 가장자리까지 거리: {detail}",
        )
    ]


# --- 텍스트 넘침 ---------------------------------------------------------------
def _check_overflow(shape: Shape) -> list[Finding]:
    """렌더링 좌표와 도형 상자가 모두 있어야 판정한다. 없으면 판정 불가로 남긴다."""
    if not _has_text(shape):
        return []
    rendered = shape.rendered_box
    box = shape.box
    if rendered is None or box is None:
        missing = "렌더링된 글자 영역" if rendered is None else "도형 상자"
        return [_geometry_unknown("layout.overflow", shape, f"{missing}을 알 수 없어 텍스트 넘침")]
    over = {
        "왼쪽": box.x - rendered.x,
        "위": box.y - rendered.y,
        "오른쪽": (rendered.x + rendered.width) - (box.x + box.width),
        "아래": (rendered.y + rendered.height) - (box.y + box.height),
    }
    exceeded = {name: amount for name, amount in over.items() if amount > TOL}
    if not exceeded:
        return []
    worst_name = max(exceeded, key=lambda n: exceeded[n])
    detail = ", ".join(f"{name} {amount:.1f}pt" for name, amount in exceeded.items())
    return [
        _finding(
            "layout.overflow",
            shape.key,
            shape,
            "warning",
            f"텍스트가 도형 영역을 벗어납니다 ({worst_name})",
            round(exceeded[worst_name], 1),
            0.0,
            f"도형 상자 대비 텍스트가 넘친 길이: {detail}",
            box=rendered,
        )
    ]


# --- 겹침 ----------------------------------------------------------------------
def _intersection(a: Box, b: Box) -> Box | None:
    left, top = max(a.x, b.x), max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    width, height = right - left, bottom - top
    if width <= TOL or height <= TOL:
        return None
    return Box(left, top, width, height)


def _contains(outer: Box, inner: Box) -> bool:
    return (
        outer.x - TOL <= inner.x
        and outer.y - TOL <= inner.y
        and outer.x + outer.width + TOL >= inner.x + inner.width
        and outer.y + outer.height + TOL >= inner.y + inner.height
    )


def _behind(back: Shape, front: Shape) -> bool | None:
    """back이 front보다 뒤에 있으면 True. 순서를 알 수 없으면 None.

    PowerPoint의 z_order는 1이 가장 뒤이고 숫자가 클수록 앞이다.
    """
    if back.z_order is None or front.z_order is None:
        return None
    return back.z_order < front.z_order


def _check_overlap(shapes: list[Shape]) -> list[Finding]:
    findings: list[Finding] = []
    for shape in shapes:
        if _has_text(shape) and _effective_box(shape) is None:
            findings.append(_geometry_unknown("layout.overlap", shape, "좌표를 확인할 수 없어 겹침"))
    for a, b in combinations(shapes, 2):
        a_text, b_text = _has_text(a), _has_text(b)
        if not (a_text or b_text):
            continue  # 장식 도형끼리의 겹침은 보지 않는다
        box_a, box_b = _effective_box(a), _effective_box(b)
        if box_a is None or box_b is None:
            continue  # 위에서 판정 불가로 이미 보고했다
        overlap = _intersection(box_a, box_b)
        if overlap is None:
            continue
        anchor, other = (a, b) if a_text else (b, a)
        anchor_box, other_box = (box_a, box_b) if a_text else (box_b, box_a)

        severity: Severity = "review"
        message = "텍스트가 다른 객체와 겹칩니다 (의도한 배치인지 확인)"
        area = overlap.width * overlap.height
        measured: float | None = round(area, 1)
        expected: float | None = 0.0
        evidence = (
            f"{anchor.name}({anchor.key}) ↔ {other.name}({other.key}): "
            f"겹친 영역 {overlap.width:.1f}×{overlap.height:.1f}pt"
        )
        if not _has_text(other) and _contains(other_box, anchor_box):
            # 글자 없는 도형이 텍스트를 완전히 덮고 있다. 뒤면 배경 패널, 앞이면 텍스트를 가린다.
            behind = _behind(other, anchor)
            if behind is True:
                continue
            if behind is False:
                message = "앞쪽 도형이 텍스트를 가립니다"
            else:
                # 앞뒤를 모르면 배경인지 가림인지 정할 수 없으므로 판정 불가로 남긴다.
                severity = "unknown"
                message = "앞뒤 순서를 알 수 없어 배경인지 가림인지 판정할 수 없습니다"
                measured = expected = None
                evidence = f"{evidence}; 두 도형의 z_order를 확인하지 못함"
        findings.append(
            _finding(
                "layout.overlap",
                f"{a.key}:{b.key}",
                anchor,
                severity,
                message,
                measured,
                expected,
                evidence,
                box=overlap,
            )
        )
    return findings


# --- 정보 밀도 -----------------------------------------------------------------
def _line_count(shape: Shape) -> tuple[int, bool]:
    """(줄 수, 렌더링 줄을 반영했는지).

    원문 개행만 세면 긴 문단 하나가 화면에서 여러 줄로 접혀도 1줄로 잡힌다.
    reader가 실제 줄(``TextSpan.kind == "line"``)을 주면 둘 중 큰 값을 쓴다.
    reader는 줄을 일정 개수까지만 읽으므로, 아주 긴 텍스트에서는 원문 개행이 더 많을 수 있다.
    """
    source = sum(1 for line in _text_of(shape).splitlines() if line.strip())
    rendered = sum(1 for span in shape.spans if span.kind == "line" and span.text.strip())
    if rendered:
        return max(source, rendered), True
    return source, False


def _check_density(slide_number: int, shapes: list[Shape], purpose: Purpose) -> list[Finding]:
    text_shapes = [s for s in shapes if _has_text(s)]
    if not text_shapes:
        return []
    limits = density_threshold(purpose)
    chars_by_shape = {s.key: len("".join(_text_of(s).split())) for s in text_shapes}
    total_chars = sum(chars_by_shape.values())
    counts = [_line_count(s) for s in text_shapes]
    total_lines = sum(count for count, _ in counts)
    rendered_used = any(rendered for _, rendered in counts)
    heaviest = max(text_shapes, key=lambda s: chars_by_shape[s.key])  # 화면에서 가리킬 대표 도형

    findings: list[Finding] = []
    if total_chars > limits.max_chars:
        findings.append(
            _finding(
                "layout.density",
                f"{slide_number}:chars",
                heaviest,
                "review",
                f"슬라이드의 글자 수가 많습니다 ({total_chars}자)",
                total_chars,
                limits.max_chars,
                f"슬라이드 {slide_number}: 공백 제외 {total_chars}자 > 기준 {limits.max_chars}자 "
                f"(도형 {len(text_shapes)}개 합계)",
            )
        )
    if total_lines > limits.max_lines:
        findings.append(
            _finding(
                "layout.density",
                f"{slide_number}:lines",
                heaviest,
                "review",
                f"슬라이드의 줄(항목) 수가 많습니다 ({total_lines}줄)",
                total_lines,
                limits.max_lines,
                f"슬라이드 {slide_number}: {total_lines}줄 > 기준 {limits.max_lines}줄 "
                f"(도형 {len(text_shapes)}개 합계, "
                f"{'자동 줄바꿈 반영' if rendered_used else '원문 개행 기준'})",
            )
        )
    return findings
