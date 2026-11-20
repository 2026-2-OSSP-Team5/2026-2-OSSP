"""슬라이드 재배치: 글자를 키우거나 넘친 글상자를 늘릴 때 슬라이드 전체를 다시 쌓는다.

문제 하나씩 고치면(글자만 키우기, 상자만 늘리기) 다른 문제가 생긴다.
  - 글자를 14pt → 18pt로 키우면 상자를 넘친다
  - 넘친 상자를 늘리면 아래 상자와 부딪힌다
이 모듈은 한 슬라이드에서 바뀌는 것을 한꺼번에 계산해 '함께 적용할 묶음' 하나로 만든다.

방법 (문서 편집기처럼 위에서 아래로 흐르게)
  1. 글상자마다 바뀐 뒤 필요한 높이를 예상한다.
       지금 글자 높이 × (새 크기 ÷ 지금 크기) × (줄 수가 늘어나는 비율)
       줄 수는 글자 폭(한글 1em, 영문·숫자 0.55em)으로 상자 안쪽 폭에 몇 줄로 접히는지 센다.
  2. 모자라는 만큼 상자를 늘린다.
  3. 늘어난 상자 아래에 있는(가로로 겹치는) 객체들은 늘어난 만큼 내린다. 내린 객체 아래도 연쇄로 내린다.
     아래 객체와의 빈 공간을 먼저 쓰고(원래 간격의 절반, 최소 MIN_GAP까지 줄어들 수 있음),
     그래도 모자란 만큼만 내린다. 글상자끼리 원래부터 겹쳐 있었다면 MIN_GAP만큼 떼어 놓는다
     (글상자가 아닌 객체와의 겹침은 디자인 의도일 수 있어 그대로 둔다).
  4. 글상자를 감싼 뒤쪽 패널(카드 배경 등)은 안의 글상자가 늘어난 만큼 같이 늘린다.
  5. 다 내려도 슬라이드 아래 안전 여백 안에 들어가지 않거나 고정 객체(바닥글·쪽 번호)와 부딪히면
     재배치하지 않고 이유를 돌려준다 → 글 줄이기·슬라이드 나누기는 사람이 판단.

움직이지 않는 것: 슬라이드 대부분을 덮는 배경 그림, 바닥글·날짜·쪽 번호, 좌표를 모르는 객체.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# PowerPoint PpPlaceholderType: 쪽 번호 13, 바닥글 15, 날짜 16
FOOTER_PLACEHOLDERS = frozenset({13, 15, 16})
BACKGROUND_AREA = 0.7  # 슬라이드 넓이의 이 비율 이상을 덮는 객체는 배경으로 보고 움직이지 않음
TOL = 0.5  # pt
PAD = 2.0  # 예상 높이에 더하는 여유 (pt)
MIN_GAP = 6.0  # 원래 겹쳐 있던 글상자끼리 떼어 놓을 때 간격 (pt)
MIN_GROW = 3.0  # 이보다 적게 모자라면 상자를 늘리지 않음 (몇 pt 때문에 슬라이드 전체를 움직이지 않게)
LATIN_EM = 0.55  # 영문·숫자·기호 한 글자 폭 (em)
DEFAULT_MARGINS = 7.2  # 위아래 안쪽 여백을 모를 때 (PowerPoint 기본 3.6pt × 2)
DEFAULT_SIDE_MARGINS = 14.4  # 좌우 안쪽 여백을 모를 때 (7.2pt × 2)


@dataclass
class FlowItem:
    """재배치에 쓰는 객체 하나 (좌표 단위 pt)."""

    shape_id: int
    left: float
    top: float
    width: float
    height: float
    is_text: bool = False
    text: str = ""
    size_pt: float | None = None  # 대표 글자 크기 (글자 수로 가중 평균)
    text_height: float | None = None  # 지금 실제 글자 높이 (PowerPoint가 그린 값)
    margins: float | None = None  # 위아래 안쪽 여백 합
    inner_width: float | None = None  # 좌우 안쪽 여백을 뺀 폭
    z_order: int | None = None
    fixed: bool = False  # 움직이지 않는 객체 (배경·바닥글)
    background: bool = False  # 슬라이드 대부분을 덮는 배경 (충돌 검사에서도 뺌)

    @property
    def bottom(self) -> float:
        return self.top + self.height


@dataclass
class Reflow:
    """재배치 결과. heights/tops는 바뀌는 객체만 담는다."""

    heights: dict[int, float] = field(default_factory=dict)  # shape_id → 새 높이
    tops: dict[int, float] = field(default_factory=dict)  # shape_id → 새 위쪽 좌표
    reason: str = ""  # 재배치할 수 없을 때 이유 (비어 있으면 성공)

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def changes(self) -> bool:
        return bool(self.heights or self.tops)


def char_em(ch: str) -> float:
    """글자 하나의 대략적인 폭 (em). 한글·한자·전각은 1, 그 밖은 LATIN_EM."""
    if ch.isspace():
        return 0.3
    return 1.0 if ord(ch) >= 0x1100 else LATIN_EM


def wrapped_lines(text: str, size_pt: float, width_pt: float) -> int:
    """text를 size_pt 크기로 폭 width_pt 상자에 넣으면 몇 줄로 접히는지 (대략)."""
    if width_pt <= 0 or size_pt <= 0:
        return max(1, len(text.splitlines()))
    total = 0
    for paragraph in text.splitlines() or [""]:
        width = sum(char_em(ch) for ch in paragraph) * size_pt
        total += max(1, math.ceil(width / width_pt - 1e-9))
    return total


def predicted_height(item: FlowItem, new_size: float | None) -> float | None:
    """글자 크기를 new_size로 바꾼 뒤 상자에 필요한 높이. 예상할 수 없으면 None."""
    if not item.is_text or item.text_height is None:
        return None
    margins = item.margins if item.margins is not None else DEFAULT_MARGINS
    text_height = item.text_height
    if new_size is not None and item.size_pt and new_size > item.size_pt + TOL:
        width = item.inner_width if item.inner_width else max(item.width - DEFAULT_SIDE_MARGINS, 1.0)
        before = wrapped_lines(item.text, item.size_pt, width)
        after = wrapped_lines(item.text, new_size, width)
        text_height *= (new_size / item.size_pt) * (after / before)
        # 줄 바꿈 위치는 글꼴마다 달라 예상이 빗나갈 수 있으므로 반 줄만큼 더 둔다
        return text_height + margins + max(PAD, new_size * 0.6)
    return text_height + margins + PAD


def reflow_slide(
    items: list[FlowItem],
    new_sizes: dict[int, float],
    grow_to: dict[int, float],
    fit: set[int] | frozenset[int] = frozenset(),
    *,
    slide_h: float,
    bottom_margin: float,
) -> Reflow:
    """new_sizes: 글자 크기를 바꿀 글상자 → 새 크기, grow_to: 이미 넘쳐서 이만큼 필요한 상자 → 높이,
    fit: 넘쳤지만 필요한 높이를 모르는 글상자 (지금 글자 높이 + 안쪽 여백으로 계산)."""
    result = Reflow()
    movable = [item for item in items if not item.fixed]
    by_id = {item.shape_id: item for item in items}

    # 1~2. 상자마다 필요한 높이
    growth: dict[int, float] = {}
    for shape_id in set(new_sizes) | set(grow_to) | set(fit):
        item = by_id.get(shape_id)
        if item is None or item.fixed:
            continue
        need = predicted_height(item, new_sizes.get(shape_id))
        if shape_id in grow_to:
            need = max(need or 0.0, grow_to[shape_id])
        if need is not None and need > item.height + MIN_GROW:
            growth[shape_id] = math.ceil(need) - item.height
    if not growth:
        return result

    # 4. 글상자를 감싼 뒤쪽 패널은 안의 글상자와 같이 늘린다
    panels: dict[int, list[int]] = {}
    for panel in movable:
        if panel.is_text:
            continue
        children = [
            child.shape_id
            for child in movable
            if child.is_text and _contains(panel, child) and _behind(panel, child)
        ]
        if children:
            panels[panel.shape_id] = children

    # 3. 위에서 아래로: 내 위에 있는 객체의 새 아래쪽 끝 + 원래 간격보다 위에 있으면 그만큼 내린다
    shift: dict[int, float] = {}
    new_height: dict[int, float] = {}
    order = sorted(movable, key=lambda item: (item.top, item.left))
    for item in order:
        down = 0.0
        for above in order:
            if above is item or not shift.keys() >= {above.shape_id}:
                continue  # 아직 위치를 정하지 않은 객체(=내 아래)는 보지 않음
            if item.shape_id in panels.get(above.shape_id, ()) or not _same_column(above, item):
                continue
            gap = item.top - above.bottom
            if gap >= -TOL and _grown(above, growth, panels, shift) > TOL:
                # 늘어난 상자 바로 아래 빈 공간은 절반(최소 MIN_GAP)까지 쓴다. 그냥 내려가는 객체끼리는 간격 유지
                gap = min(gap, max(MIN_GAP, gap * 0.5))
            if gap < -TOL:  # 원래부터 겹쳐 있음
                if not (above.is_text and item.is_text) or item.top <= above.top + TOL:
                    continue
                if above.shape_id not in growth and shift[above.shape_id] <= TOL:
                    continue  # 바뀌지 않은 객체와의 원래 겹침은 이 재배치 탓이 아님
                gap = MIN_GAP
            above_bottom = (
                above.top + shift[above.shape_id] + above.height + _grown(above, growth, panels, shift)
            )
            down = max(down, above_bottom + gap - item.top)
        shift[item.shape_id] = max(down, 0.0)
        new_height[item.shape_id] = item.height + growth.get(item.shape_id, 0.0)
    for panel_id, children in panels.items():
        panel = by_id[panel_id]
        extra = max(
            (by_id[c].bottom + shift[c] + growth.get(c, 0.0)) - (by_id[c].bottom + shift[panel_id])
            for c in children
        )
        if extra > TOL:
            new_height[panel_id] = panel.height + extra

    # 5. 슬라이드 안에 들어가는지, 고정 객체와 새로 부딪히지 않는지
    limit = slide_h - bottom_margin
    for item in movable:
        top = item.top + shift[item.shape_id]
        bottom = top + new_height[item.shape_id]
        if bottom > max(limit, item.bottom) + TOL:
            short = bottom - max(limit, item.bottom)
            result.reason = (
                f"다시 배치해도 슬라이드 아래로 {short:.0f}pt 넘침 (객체 {item.shape_id}) "
                "→ 글 줄이기·슬라이드 나누기는 사람이 판단"
            )
            return result
        moved = (item.left, top, item.width, new_height[item.shape_id])
        for fixed in items:
            if not fixed.fixed or fixed.background:
                continue
            if _overlaps(moved, _rect(fixed)) and not _overlaps(_rect(item), _rect(fixed)):
                result.reason = (
                    f"다시 배치하면 고정 객체(ID {fixed.shape_id}, 바닥글 등)와 겹침 → 사람이 판단"
                )
                return result

    for item in movable:
        if shift[item.shape_id] > TOL:
            result.tops[item.shape_id] = round(item.top + shift[item.shape_id], 2)
        if new_height[item.shape_id] > item.height + TOL:
            result.heights[item.shape_id] = round(new_height[item.shape_id], 2)
    return result


# ---------------------------------------------------------------- 도우미


def _grown(
    item: FlowItem, growth: dict[int, float], panels: dict[int, list[int]], shift: dict[int, float]
) -> float:
    """item의 아래쪽 끝이 원래보다 얼마나 내려가는지 (자기 이동 제외)."""
    if item.shape_id in panels:
        children = panels[item.shape_id]
        return max(
            0.0, max(shift.get(c, 0.0) + growth.get(c, 0.0) for c in children) - shift.get(item.shape_id, 0.0)
        )
    return growth.get(item.shape_id, 0.0)


def _same_column(above: FlowItem, below: FlowItem) -> bool:
    """가로 범위가 겹치면 같은 세로 흐름에 있다고 본다."""
    return min(above.left + above.width, below.left + below.width) - max(above.left, below.left) > TOL


def _contains(outer: FlowItem, inner: FlowItem) -> bool:
    return (
        outer.left - TOL <= inner.left
        and outer.top - TOL <= inner.top
        and outer.left + outer.width + TOL >= inner.left + inner.width
        and outer.bottom + TOL >= inner.bottom
    )


def _behind(back: FlowItem, front: FlowItem) -> bool:
    return back.z_order is not None and front.z_order is not None and back.z_order < front.z_order


def _rect(item: FlowItem) -> tuple[float, float, float, float]:
    return (item.left, item.top, item.width, item.height)


def _overlaps(a: tuple, b: tuple) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return min(ax + aw, bx + bw) - max(ax, bx) > TOL and min(ay + ah, by + bh) - max(ay, by) > TOL
