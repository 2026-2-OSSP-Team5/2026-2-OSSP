"""같은 자리 일괄 적용.

여러 슬라이드의 '정확히 같은 자리'에 있는 같은 종류의 개체(쪽 번호, 머리글의 소제목, 바닥글 등)는
대개 같은 서식을 공유한다. 그중 하나에 수정 후보가 있으면 나머지에도 똑같이 적용할 수 있게 한다.

같은 자리 = 다른 슬라이드에 있고, 상자 좌표(왼쪽·위·너비·높이)가 1pt 이내로 같고,
            자리 표시자 종류(제목·본문·쪽 번호…)가 같은 글 있는 개체.
같은 수정 = 같은 속성, 같은 원래 값, 같은 바꿀 값 (최소/권장 수정 구분도 같음).

1) extend_same_place: 같은 자리 개체의 현재 값이 원래 값과 같은데 그 개체에는 후보가 없으면
   (그 쪽에는 판정이 없었던 경우 등) 같은 수정 후보를 만들어 붙인다.
2) same_place_sets: '같은 자리 · 같은 수정' 후보 묶음 (화면에서 '일괄 적용' 체크 하나로 켜고 끔).
개별 후보는 그대로 남으므로 하나씩 적용하는 기존 방식도 그대로 쓸 수 있다.
"""

from __future__ import annotations

from dataclasses import replace

POSITION_TOLERANCE_PT = 1.0
# 일괄 적용하는 속성: 글자 서식 + 상자 높이·너비
#   같은 자리의 쪽 번호·머리글은 상자 크기도 같으므로 높이·너비도 똑같이 바꾸는 게 맞다
#   (쪽마다 따로 늘리면 쪽 번호 상자 크기가 쪽마다 달라짐).
#   위치 이동(shape.position)은 쪽마다 planner가 겹침을 확인해 만든 후보끼리만 묶는다
#   (다른 쪽에 후보를 새로 복사하지는 않음: 쪽마다 주변 개체가 달라 겹칠 수 있음)
BATCH_PROPERTIES = ("font.size", "font.color", "font.bold", "shape.height", "shape.width", "shape.position")
COPY_PROPERTIES = ("font.size", "font.color", "font.bold", "shape.height", "shape.width")
_SIZE_PROPERTIES = {"shape.height": "height", "shape.width": "width"}
RECOMMENDED_MARK = "~rec"


def _usable(shape) -> bool:
    """판단에 필요한 정보(주소·슬라이드·상자·글자)를 가진 개체인지 (테스트용 가짜 객체 등은 건너뜀)."""
    return all(hasattr(shape, name) for name in ("key", "slide_number", "box", "placeholder_type", "runs"))


def _same_place(first, second) -> bool:
    if first.slide_number == second.slide_number or first.box is None or second.box is None:
        return False
    if first.placeholder_type != second.placeholder_type:
        return False
    a, b = first.box, second.box
    return all(
        abs(x - y) <= POSITION_TOLERANCE_PT
        for x, y in ((a.x, b.x), (a.y, b.y), (a.width, b.width), (a.height, b.height))
    )


def _simple_key(key: str) -> bool:
    """'3:12'처럼 슬라이드의 맨 위 개체 (그룹 안·표 칸·차트 글자는 제외)."""
    parts = str(key).split(":")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


def _base_id(proposal) -> str:
    return str(proposal.id).split("#", 1)[0]


def _signature(proposal) -> tuple:
    return (
        proposal.property,
        _norm(proposal.before),
        _norm(proposal.after),
        RECOMMENDED_MARK in _base_id(proposal),
    )


def _norm(value):
    if isinstance(value, str):
        return value.upper().lstrip("#")
    if isinstance(value, (list, tuple)):
        return tuple(_norm(item) for item in value)
    return value


def _has_value(shape, proposal) -> bool:
    """이 개체에 같은 수정을 해도 되는지 (지금 값이 원래 값과 같은지)."""
    if proposal.property in _SIZE_PROPERTIES:
        # 상자 높이·너비: 그 개체의 지금 크기가 원래 값과 같을 때만 (같은 자리면 보통 같음)
        size = getattr(shape.box, _SIZE_PROPERTIES[proposal.property], None)
        try:
            return size is not None and abs(float(size) - float(proposal.before)) <= POSITION_TOLERANCE_PT
        except (TypeError, ValueError):
            return False
    runs = [run for run in shape.runs if run.text.strip()]
    if not runs:
        return False
    if proposal.property == "font.size":
        sizes = [run.font_size_pt for run in runs if run.font_size_pt is not None]
        return bool(sizes) and abs(min(sizes) - float(proposal.before)) <= 0.05
    colors = {"".join(f"{c:02X}" for c in run.color) for run in runs if run.color is not None}
    if proposal.property == "font.color":
        return _norm(proposal.before) in colors
    if proposal.property == "font.bold":
        return any(
            run.color is not None
            and "".join(f"{c:02X}" for c in run.color) == _norm(proposal.before)
            and run.bold is not True
            for run in runs
        )
    return False


def _batchable(proposal) -> bool:
    return (
        proposal.property in BATCH_PROPERTIES
        and "#" not in str(proposal.id)
        and _simple_key(proposal.shape_key)
    )


def extend_same_place(deck, proposals: list) -> list:
    """같은 자리 개체에 같은 수정 후보를 덧붙인 목록 (원래 후보는 그대로, 순서도 유지)."""
    shapes = {
        shape.key: shape
        for shape in getattr(deck, "shapes", ())
        if _usable(shape) and str(getattr(shape, "text", "")).strip() and getattr(shape, "supported", False)
    }
    by_target = {}
    for proposal in proposals:
        by_target.setdefault(
            (proposal.shape_key, proposal.property, RECOMMENDED_MARK in _base_id(proposal)), []
        ).append(proposal)
    ids = {proposal.id for proposal in proposals}
    extra = []
    for proposal in proposals:
        source = shapes.get(proposal.shape_key)
        if source is None or not _batchable(proposal) or proposal.property not in COPY_PROPERTIES:
            continue
        for key, shape in shapes.items():
            if not _simple_key(key) or not _same_place(source, shape):
                continue
            existing = by_target.get((key, proposal.property, RECOMMENDED_MARK in _base_id(proposal)), [])
            if proposal.property not in ("font.size", *_SIZE_PROPERTIES):
                existing = [item for item in existing if _norm(item.before) == _norm(proposal.before)]
            if existing or not _has_value(shape, proposal):
                continue  # 이미 후보가 있음(같든 다르든 그 후보를 존중) 또는 값이 달라 같은 서식이 아님
            new_id = _base_id(proposal).replace(f"@{proposal.shape_key}", f"@{key}", 1)
            while new_id in ids:
                new_id += "~same"
            copy = replace(proposal, id=new_id, shape_key=key)
            ids.add(new_id)
            extra.append(copy)
            by_target.setdefault((key, proposal.property, RECOMMENDED_MARK in _base_id(proposal)), []).append(
                copy
            )
    return list(proposals) + extra


def same_place_sets(deck, proposals: list) -> list[tuple[str, ...]]:
    """'같은 자리 · 같은 수정' 후보 id 묶음들 (2개 이상, 서로 다른 슬라이드). 첫 후보 순서대로.

    속도: 후보가 수천 개여도 빠르도록 '같은 수정'(속성·전·후 값)끼리 먼저 나눈 뒤 그 안에서만 자리를 비교한다.
    (전에는 모든 후보 쌍을 비교해서 24쪽 문서에서 렌즈를 그릴 때마다 몇 초씩 화면이 멈췄음)
    """
    shapes = {shape.key: shape for shape in getattr(deck, "shapes", ()) if _usable(shape)}
    by_signature: dict[tuple, list] = {}
    for item in proposals:
        if _batchable(item) and item.shape_key in shapes:
            by_signature.setdefault(_signature(item), []).append(item)
    seen: set[str] = set()
    sets = []
    for candidates in by_signature.values():
        if len(candidates) < 2:
            continue
        for proposal in candidates:
            if proposal.id in seen:
                continue
            source = shapes[proposal.shape_key]
            members = [proposal]
            used_keys = {proposal.shape_key}
            for other in candidates:
                if other.id == proposal.id or other.id in seen or other.shape_key in used_keys:
                    continue  # 한 개체에는 하나만 (같은 개체의 다른 후보는 묶지 않음)
                if _same_place(source, shapes[other.shape_key]):
                    members.append(other)
                    used_keys.add(other.shape_key)
            slides = {shapes[m.shape_key].slide_number for m in members}
            if len(slides) >= 2:
                sets.append(tuple(m.id for m in members))
                seen.update(m.id for m in members)
    # 원래 순서(첫 후보가 목록에 나온 순서)대로
    order = {item.id: index for index, item in enumerate(proposals)}
    sets.sort(key=lambda ids: order.get(ids[0], 0))
    return sets
