"""Overlap requires review and does not prove unintended placement."""

import re
from collections import defaultdict
from itertools import combinations

from slideguard.domain.models import Box, DeckSnapshot, Finding, Severity, Shape
from slideguard.domain.policy import GEOMETRY_TOLERANCE_PT, AnalysisPolicy, DensityThreshold

from .scope import target_shapes

TOL = GEOMETRY_TOLERANCE_PT
HANG_EM = 0.5  # 좌우 넘침 허용: 글자 크기의 절반 (줄 끝 문장 부호가 밖으로 나가는 정도)
DESCENT_EM = 0.1  # 위아래 넘침 허용: 글자 크기의 10% (글꼴 아래 여유)


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
            findings.extend(_check_margin(deck, shape, policy.margin_ratio))
            findings.extend(_check_overflow(shape))

        by_slide: dict[int, list[Shape]] = defaultdict(list)
        for shape in shapes:
            by_slide[shape.slide_number].append(shape)
        for slide_number in sorted(by_slide):
            slide_shapes = sorted(by_slide[slide_number], key=lambda s: s.shape_id)
            findings.extend(_check_overlap(slide_shapes))
            findings.extend(_check_card_off_slide(deck, slide_shapes))
            findings.extend(_check_density(slide_number, slide_shapes, policy.density_limits, deck.height_pt))
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
    return shape.text_bounds or shape.box


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
def _check_margin(deck: DeckSnapshot, shape: Shape, margin_ratio: float) -> list[Finding]:
    if not _has_text(shape):
        return []
    box = _effective_box(shape)
    if box is None:
        return [_geometry_unknown("layout.margin", shape, "좌표를 확인할 수 없어 안전 여백")]
    margin_x = deck.width_pt * margin_ratio
    margin_y = deck.height_pt * margin_ratio
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
        message = f"텍스트가 안전 여백({margin_ratio:.0%}) 안쪽에 있지 않습니다"
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


# 카드처럼 글을 감싼 도형이 슬라이드 밖으로 이만큼 넘게 나가면 문제로 본다 (슬라이드 너비·높이 비율)
CARD_OFF_SLIDE_RATIO = 0.01
# 이보다 넓은(높은) 도형은 일부러 가장자리까지 꽉 채운 배경·띠로 보고 검사하지 않는다
FULL_BLEED_RATIO = 0.9


def _check_card_off_slide(deck: DeckSnapshot, shapes: list[Shape]) -> list[Finding]:
    """글상자를 감싼 카드·패널이 슬라이드 밖으로 나가는지 (함정 PPT 2쪽 세 번째 카드).

    글자 자체는 슬라이드 안이라 여백 검사에 걸리지 않지만, 슬라이드 쇼에서는 카드 테두리가 잘려
    '실수로 밀린 슬라이드'처럼 보인다. 배경 사진·띠처럼 일부러 가장자리 밖까지 깐 도형은
    슬라이드 너비(높이)의 90% 이상이거나 글상자를 감싸지 않으므로 빼고 본다.
    """
    texts = [shape for shape in shapes if _has_text(shape) and _effective_box(shape) is not None]
    findings = []
    for shape in shapes:
        box = shape.box
        if _has_text(shape) or box is None:
            continue
        if box.width >= deck.width_pt * FULL_BLEED_RATIO or box.height >= deck.height_pt * FULL_BLEED_RATIO:
            continue
        if not any(_contains(box, _effective_box(text)) for text in texts if text.key != shape.key):
            continue
        sides = {
            "왼쪽": -box.x,
            "오른쪽": box.x + box.width - deck.width_pt,
            "위": -box.y,
            "아래": box.y + box.height - deck.height_pt,
        }
        limit = {"왼쪽": deck.width_pt, "오른쪽": deck.width_pt, "위": deck.height_pt, "아래": deck.height_pt}
        outside = {
            name: amount for name, amount in sides.items() if amount > limit[name] * CARD_OFF_SLIDE_RATIO
        }
        if not outside:
            continue
        name = max(outside, key=lambda key: outside[key])
        findings.append(
            _finding(
                "layout.margin",
                f"{shape.key}:card",
                shape,
                "warning",
                f"글을 감싼 도형이 슬라이드 밖으로 나갑니다 ({name})",
                round(-outside[name], 1),
                0.0,
                "슬라이드 밖으로 나간 길이: " + ", ".join(f"{k} {v:.1f}pt" for k, v in outside.items()),
                box=box,
            )
        )
    return findings


# --- 텍스트 넘침 ---------------------------------------------------------------
def _check_overflow(shape: Shape) -> list[Finding]:
    """렌더링 좌표와 도형 상자가 모두 있어야 판정한다. 없으면 판정 불가로 남긴다."""
    if not _has_text(shape):
        return []
    if shape.locator is not None and shape.locator.kind == "table_cell":
        # 표 칸은 글자가 늘면 PowerPoint가 행 높이를 저절로 늘리므로 넘치지 않는다.
        # (칸 글자 위치는 픽셀 차이로 재는 근사값이라, 잘못 재면 '655pt 넘침' 같은 오판정이 난다)
        return []
    rendered = shape.text_bounds
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
    # PowerPoint는 줄 끝 쉼표·마침표를 상자 밖으로 조금 내보내고(문장 부호 끝 맞춤),
    # 글꼴의 아래 여유(내림 획 자리)도 상자 끝에 살짝 걸친다. 이런 미세한 넘침은 화면에서 잘리지 않는다.
    size = max((run.font_size_pt or 0.0 for run in shape.runs), default=0.0)
    allowed = {
        "왼쪽": max(TOL, size * HANG_EM),
        "오른쪽": max(TOL, size * HANG_EM),
        "위": max(TOL, size * DESCENT_EM),
        "아래": max(TOL, size * DESCENT_EM),
    }
    exceeded = {name: amount for name, amount in over.items() if amount > allowed[name]}
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


def _ink_boxes(shape: Shape, fallback: Box) -> list[Box]:
    """겹침을 따질 실제 영역: 글자가 있으면 줄마다의 상자, 없으면 도형 상자.

    PowerPoint에서 줄 위치(TextSpan kind="line")를 읽었으면 그것을 쓰고, 못 읽었으면
    전체 글자 영역(fallback)을 쓴다. 표 셀의 줄 좌표는 셀 기준이라 쓰지 않는다.
    """
    if _has_text(shape) and not (shape.locator is not None and shape.locator.kind == "table_cell"):
        lines = [span.box for span in shape.spans if span.kind == "line" and span.box and span.text.strip()]
        if lines:
            return lines
    return [fallback]


def _ink_overlap(first: list[Box], second: list[Box]) -> tuple[Box | None, float]:
    """두 영역 목록이 겹치는 부분을 감싼 상자와 실제로 겹친 넓이 합."""
    parts = [part for a in first for b in second if (part := _intersection(a, b)) is not None]
    if not parts:
        return None, 0.0
    left = min(part.x for part in parts)
    top = min(part.y for part in parts)
    right = max(part.x + part.width for part in parts)
    bottom = max(part.y + part.height for part in parts)
    return Box(left, top, right - left, bottom - top), sum(part.width * part.height for part in parts)


def _owns_content(container: Shape, content: Shape) -> bool:
    outer, inner = container.locator, content.locator
    return (
        outer is not None
        and inner is not None
        and outer.kind == "shape"
        and outer != inner
        and inner.shape_path[: len(outer.shape_path)] == outer.shape_path
    )


def _check_overlap(shapes: list[Shape]) -> list[Finding]:
    findings: list[Finding] = []
    for shape in shapes:
        if _has_text(shape) and _effective_box(shape) is None:
            findings.append(_geometry_unknown("layout.overlap", shape, "좌표를 확인할 수 없어 겹침"))
    for a, b in combinations(shapes, 2):
        # A table/group encloses its own cells/children by construction.
        if _owns_content(a, b) or _owns_content(b, a):
            continue
        a_text, b_text = _has_text(a), _has_text(b)
        if not (a_text or b_text):
            continue  # 장식 도형끼리의 겹침은 보지 않는다
        box_a, box_b = _effective_box(a), _effective_box(b)
        if box_a is None or box_b is None:
            continue  # 위에서 판정 불가로 이미 보고했다
        if _intersection(box_a, box_b) is None:
            continue
        anchor, other = (a, b) if a_text else (b, a)
        anchor_box, other_box = (box_a, box_b) if a_text else (box_b, box_a)
        # 글상자 테두리(줄들을 감싼 사각형)끼리가 아니라 실제 글자 줄끼리 겹치는지 본다.
        # 예) 위 상자의 짧은 마지막 줄 옆으로 아래 상자가 올라와도 글자는 안 겹침 → 겹침 아님
        overlap, area = _ink_overlap(_ink_boxes(anchor, anchor_box), _ink_boxes(other, other_box))
        if overlap is None:
            continue
        if not _has_text(other) and _behind(other, anchor) is True:
            # 글자 뒤에 깔린 도형(말풍선·카드·사진 등)은 배경이다. 글씨가 잘 보이는지는 명암비 검사가 본다.
            continue

        severity: Severity = "review"
        message = "텍스트가 다른 객체와 겹칩니다 (의도한 배치인지 확인)"
        measured: float | None = round(area, 1)
        expected: float | None = 0.0
        evidence = (
            f"{anchor.name}({anchor.key}) ↔ {other.name}({other.key}): "
            f"겹친 영역 {overlap.width:.1f}×{overlap.height:.1f}pt"
        )
        if not _has_text(other):
            behind = _behind(other, anchor)  # 뒤에 깔린 경우는 위에서 이미 제외했다
            if behind is False:
                message = "앞쪽 도형이 텍스트를 가립니다"
            elif _contains(other_box, anchor_box):
                # 글자를 완전히 덮는데 앞뒤를 모르면 배경인지 가림인지 정할 수 없으므로 판정 불가로 남긴다.
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
# 눈에 보이지 않는 글자 (폭 없는 공백 등). 이런 글자만 있는 줄은 빈 줄로 본다
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u3000"), None)
# 정보 밀도에서 세지 않는 것: 차트 글자(축 눈금·데이터 레이블 등)와 쪽 번호·바닥글·날짜
_DENSITY_KINDS = frozenset({"shape", "table_cell"})
_FOOTER_PLACEHOLDERS = frozenset({13, 15, 16})


def _visible(line: str) -> bool:
    """공백·폭 없는 글자만 있는 줄이 아닌지."""
    return bool(line.translate(_INVISIBLE).strip())


def _counts_for_density(shape: Shape, slide_height: float | None = None) -> bool:
    """청중이 '읽을 내용'으로 세는 글인지 (함정 PPT 3쪽: 그래프 축 눈금 12개가 12줄로 잡힘)."""
    if shape.locator is not None and shape.locator.kind not in _DENSITY_KINDS:
        return False
    if shape.placeholder_type in _FOOTER_PLACEHOLDERS:
        return False
    # '01', '02.' 같은 번호 표시는 읽을 내용이 아니라 장식 (함정 PPT 각 쪽 왼쪽 위 번호)
    label = "".join(_text_of(shape).translate(_INVISIBLE).split())
    page = re.fullmatch(r"(\d{1,4})/(\d{1,4})", label)
    if (
        page
        and int(page[1]) == shape.slide_number
        and int(page[2]) >= shape.slide_number
        and slide_height
        and shape.box
        and (shape.box.y <= slide_height * 0.15 or shape.box.y >= slide_height * 0.85)
    ):
        return False
    return not (len(label) <= 3 and all(ch.isdigit() or ch in ".-#()" for ch in label))


def _line_count(shape: Shape) -> tuple[int, bool]:
    """(줄 수, 렌더링 줄을 반영했는지).

    원문 개행만 세면 긴 문단 하나가 화면에서 여러 줄로 접혀도 1줄로 잡힌다.
    reader가 실제 줄(``TextSpan.kind == "line"``)을 주면 둘 중 큰 값을 쓴다.
    reader는 줄을 일정 개수까지만 읽으므로, 아주 긴 텍스트에서는 원문 개행이 더 많을 수 있다.
    """
    source = sum(1 for line in _text_of(shape).splitlines() if _visible(line))
    rendered = sum(1 for span in shape.spans if span.kind == "line" and _visible(span.text))
    if rendered:
        return max(source, rendered), True
    return source, False


DENSITY_WARNING_FACTOR = 2  # 기준의 2배 이상이면 '검토'가 아니라 '문제'(발표 화면으로 읽을 수 없는 양)


def _density_severity(count: int, limit: int) -> Severity:
    return "warning" if count >= limit * DENSITY_WARNING_FACTOR else "review"


def _check_density(
    slide_number: int, shapes: list[Shape], limits: DensityThreshold, slide_height: float | None = None
) -> list[Finding]:
    text_shapes = [s for s in shapes if _has_text(s) and _counts_for_density(s, slide_height)]
    if not text_shapes:
        return []
    chars_by_shape = {s.key: len("".join(_text_of(s).translate(_INVISIBLE).split())) for s in text_shapes}
    total_chars = sum(chars_by_shape.values())
    counts = [_line_count(s) for s in text_shapes]
    total_lines = sum(count for count, _ in counts)
    rendered_used = any(rendered for _, rendered in counts)
    heaviest = max(text_shapes, key=lambda s: chars_by_shape[s.key])  # 화면에서 가리킬 대표 도형
    boxes = [box for s in text_shapes if (box := _effective_box(s)) is not None]
    bounds = None
    if boxes:
        left, top = min(b.x for b in boxes), min(b.y for b in boxes)
        bounds = Box(
            left,
            top,
            max(b.x + b.width for b in boxes) - left,
            max(b.y + b.height for b in boxes) - top,
        )
    breakdown = " · ".join(f"{s.name}: {count}줄" for s, (count, _) in zip(text_shapes, counts, strict=True))

    findings: list[Finding] = []
    if total_chars > limits.max_chars:
        findings.append(
            _finding(
                "layout.density",
                f"{slide_number}:chars",
                heaviest,
                _density_severity(total_chars, limits.max_chars),
                f"슬라이드의 글자 수가 많습니다 ({total_chars}자)",
                total_chars,
                limits.max_chars,
                f"슬라이드 {slide_number}: 공백 제외 {total_chars}자 > 기준 {limits.max_chars}자 "
                f"(도형 {len(text_shapes)}개 합계)",
                box=bounds,
            )
        )
    if total_lines > limits.max_lines:
        findings.append(
            _finding(
                "layout.density",
                f"{slide_number}:lines",
                heaviest,
                _density_severity(total_lines, limits.max_lines),
                f"슬라이드의 줄(항목) 수가 많습니다 ({total_lines}줄)",
                total_lines,
                limits.max_lines,
                f"슬라이드 {slide_number}: {total_lines}줄 > 기준 {limits.max_lines}줄 "
                f"(도형 {len(text_shapes)}개 합계, "
                f"{'자동 줄바꿈 반영' if rendered_used else '원문 개행 기준'}). {breakdown}",
                box=bounds,
            )
        )
    return findings
