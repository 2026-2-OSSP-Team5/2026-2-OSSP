"""슬라이드 간 디자인 일관성 (계획서 개발 계획 '슬라이드 간 디자인 일관성 평가').

같은 덱 안에서 대부분의 슬라이드가 지키는 '약속'에서 혼자 벗어난 슬라이드를 찾는다.
  1) 제목 위치: 제목 상자의 왼쪽·위 좌표가 다른 슬라이드와 어긋남 (넘길 때 제목이 '튀는' 슬라이드)
  2) 제목 크기: 제목 글자 크기가 다른 슬라이드와 다름
  3) 본문 크기: 본문 글자 크기가 다른 슬라이드보다 눈에 띄게 작거나 큼

약속 = 본문 슬라이드들의 중앙값. 다음일 때만 판정한다 (덱에 약속이 없으면 판정하지 않음).
  - 비교할 본문 슬라이드가 MIN_SLIDES장 이상
  - 그 가운데 AGREE_SHARE 이상이 중앙값 근처(허용 오차 안)에 모여 있음
표지·목차·출처·마무리 슬라이드는 원래 모양이 달라 빼고 본다.
모두 '검토'로 낸다 (의도한 변화일 수 있음). 제목 위치·크기는 중앙값에 맞추는 수정 후보를 만든다.
"""

from __future__ import annotations

from statistics import median

from slideguard.domain.models import DeckSnapshot, Finding, Shape
from slideguard.domain.policy import BODY_PLACEHOLDERS, TITLE_PLACEHOLDERS, AnalysisPolicy

from .scope import target_shapes
from .slide_kinds import classify

MIN_SLIDES = 4  # 이보다 적으면 '약속'을 말하기 어려움
AGREE_SHARE = 0.6  # 이 비율 이상이 중앙값 근처에 있어야 약속이 있다고 봄
POSITION_TOLERANCE = 0.02  # 제목 위치 허용 오차: 슬라이드 너비·높이의 2%
TITLE_SIZE_TOLERANCE = 0.1  # 제목 크기 허용 오차: 10%
BODY_SIZE_TOLERANCE = 0.2  # 본문 크기 허용 오차: 20%


class ConsistencyRule:
    id = "consistency"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        if not policy.consistency:
            return []
        kinds = classify(deck)
        body_slides = {number for number, kind in kinds.items() if kind == "body"}
        titles: dict[int, Shape] = {}
        bodies: dict[int, float] = {}
        for shape in target_shapes(deck, policy):
            if shape.slide_number not in body_slides or not shape.supported or not shape.text.strip():
                continue
            if shape.placeholder_type in TITLE_PLACEHOLDERS and shape.box is not None:
                titles.setdefault(shape.slide_number, shape)
            elif shape.placeholder_type in BODY_PLACEHOLDERS:
                size = _main_size(shape)
                if size is not None:
                    bodies.setdefault(shape.slide_number, size)

        findings: list[Finding] = []
        findings.extend(_title_position(deck, titles))
        findings.extend(_title_size(titles))
        findings.extend(_body_size(bodies, deck))
        return findings


# ---------------------------------------------------------------- 도우미


def _main_size(shape: Shape) -> float | None:
    """상자에서 가장 많은 글자가 쓰는 크기 (본문의 '대표 크기')."""
    counts: dict[float, int] = {}
    for run in shape.runs:
        if run.text.strip() and run.font_size_pt is not None:
            counts[run.font_size_pt] = counts.get(run.font_size_pt, 0) + len(run.text.strip())
    if not counts:
        return None
    return max(counts, key=lambda size: counts[size])


def _convention(values: list[float], tolerance: float) -> float | None:
    """값들의 중앙값이 '약속'이면 그 값. 약속이 없으면 None."""
    if len(values) < MIN_SLIDES:
        return None
    center = median(values)
    near = sum(1 for value in values if abs(value - center) <= tolerance)
    return center if near / len(values) >= AGREE_SHARE else None


def _finding(rule: str, shape: Shape, message: str, measured, expected, evidence: str) -> Finding:
    return Finding(
        id=f"consistency.{rule}:{shape.key}",
        rule_id=f"consistency.{rule}",
        shape_key=shape.key,
        severity="review",
        message=message,
        measured=measured,
        expected=expected,
        evidence=evidence,
        box=shape.text_bounds or shape.box,
    )


def _title_position(deck: DeckSnapshot, titles: dict[int, Shape]) -> list[Finding]:
    dx, dy = deck.width_pt * POSITION_TOLERANCE, deck.height_pt * POSITION_TOLERANCE
    left = _convention([shape.box.x for shape in titles.values()], dx)
    top = _convention([shape.box.y for shape in titles.values()], dy)
    if left is None or top is None:
        return []
    found = []
    for number, shape in sorted(titles.items()):
        off_x, off_y = shape.box.x - left, shape.box.y - top
        if abs(off_x) <= dx and abs(off_y) <= dy:
            continue
        found.append(
            _finding(
                "title_position",
                shape,
                "제목 위치가 다른 슬라이드와 다릅니다",
                f"{shape.box.x:g},{shape.box.y:g}",
                f"{left:g},{top:g}",
                f"슬라이드 {number} 제목 ({shape.box.x:.0f}, {shape.box.y:.0f})pt / 다른 본문 슬라이드 "
                f"{len(titles)}장의 중앙값 ({left:.0f}, {top:.0f})pt. 넘길 때 제목이 흔들려 보입니다",
            )
        )
    return found


def _title_size(titles: dict[int, Shape]) -> list[Finding]:
    sizes = {number: size for number, shape in titles.items() if (size := _main_size(shape)) is not None}
    if not sizes:
        return []
    center = median(sizes.values())
    if _convention(list(sizes.values()), center * TITLE_SIZE_TOLERANCE) is None:
        return []
    found = []
    for number, size in sorted(sizes.items()):
        if abs(size - center) <= center * TITLE_SIZE_TOLERANCE:
            continue
        found.append(
            _finding(
                "title_size",
                titles[number],
                f"제목 글자 크기가 다른 슬라이드와 다릅니다 ({size:g}pt)",
                size,
                center,
                f"슬라이드 {number} 제목 {size:g}pt / 다른 본문 슬라이드 {len(sizes)}장의 중앙값 {center:g}pt",
            )
        )
    return found


def _body_size(bodies: dict[int, float], deck: DeckSnapshot) -> list[Finding]:
    if not bodies:
        return []
    center = median(bodies.values())
    if _convention(list(bodies.values()), center * BODY_SIZE_TOLERANCE) is None:
        return []
    shapes = {
        shape.slide_number: shape
        for shape in deck.shapes
        if shape.placeholder_type in BODY_PLACEHOLDERS and shape.text.strip()
    }
    found = []
    for number, size in sorted(bodies.items()):
        if abs(size - center) <= center * BODY_SIZE_TOLERANCE or number not in shapes:
            continue
        found.append(
            _finding(
                "body_size",
                shapes[number],
                f"본문 글자 크기가 다른 슬라이드와 다릅니다 ({size:g}pt)",
                size,
                center,
                f"슬라이드 {number} 본문 {size:g}pt / 다른 본문 슬라이드 {len(bodies)}장의 중앙값 {center:g}pt. "
                "내용이 많아 줄인 것이라면 슬라이드를 나누는 편이 좋습니다",
            )
        )
    return found
