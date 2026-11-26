"""Deterministic paragraph partitioning; no rewriting, network or font estimates.

This module builds indivisible units and partitions *measured* unit heights. The
PowerPoint adapter supplies measurements, renders a candidate and validates it.
"""

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256

from slideguard.domain.policy import TITLE_PLACEHOLDERS, Strength, slide_scale

FOOTERS = frozenset({13, 15, 16})
MAX_PAGES = 20
MAX_UNITS = 256
VERSION = 1


class SplitUnavailable(ValueError):
    pass


def canonical_text(text):
    return text.replace("\r\n", "\r").replace("\n", "\r")


@dataclass(frozen=True)
class Fragment:
    shape_id: int
    start: int
    end: int
    text: str
    whole: bool = False
    properties: tuple[tuple[str, str | int | float | bool], ...] = ()


@dataclass(frozen=True)
class Unit:
    key: str
    fragments: tuple[Fragment, ...]
    object_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SplitDraft:
    slide: int
    anchor_id: int
    title_ids: tuple[int, ...]
    repeated_ids: tuple[int, ...]
    units: tuple[Unit, ...]
    font_pt: float
    line_spacing: float
    margin_ratio: float
    body_top: float
    body_bottom: float
    width: float
    left: float
    gap: float
    source_text: tuple[tuple[int, str], ...]
    once_ids: tuple[int, ...] = ()

    def payload(self):
        data = asdict(self)
        data["version"] = VERSION
        return data


def _paragraphs(shape):
    text = canonical_text(shape.text)
    paragraphs = [p for p in shape.spans if p.kind == "paragraph"]
    if not paragraphs or len(paragraphs) >= 1024:
        raise SplitUnavailable("문단 경계와 들여쓰기를 완전하게 읽지 못해 분할하지 않았습니다.")
    offset, result = 0, []
    for paragraph in paragraphs:
        value = canonical_text(paragraph.text)
        if not value or text[offset : offset + len(value)] != value:
            raise SplitUnavailable("문단을 합친 내용이 원문과 일치하지 않습니다.")
        props = dict(paragraph.properties)
        if props.get("BulletType") == 2 and not 1 <= props.get("BulletNumber", 0) <= 32767:
            raise SplitUnavailable("자동 번호 목록의 원래 번호를 확인하지 못했습니다.")
        fragment = Fragment(
            shape.shape_id, offset, offset + len(value), value, properties=paragraph.properties
        )
        result.append((fragment, props))
        offset += len(value)
    if offset != len(text):
        # Office may omit a final paragraph separator from Paragraphs().Text.
        if text[offset:] == "\r" and result:
            fragment, props = result[-1]
            result[-1] = (
                Fragment(
                    fragment.shape_id,
                    fragment.start,
                    len(text),
                    text[fragment.start :],
                    properties=fragment.properties,
                ),
                props,
            )
        else:
            raise SplitUnavailable("문단 정보가 본문 전체를 포함하지 않습니다.")
    return result


def _heading(fragment, props, default_size):
    value = fragment.text.strip()
    return (
        bool(value)
        and len(value) <= 80
        and not props.get("bullet")
        and (
            value.endswith((":", "："))
            or props.get("Bold") == -1
            or props.get("Size", 0) >= default_size * 1.15
        )
    )


def _units(shape, default_size):
    paragraphs = _paragraphs(shape)
    groups, current = [], []
    section = False
    root_indent = 1
    for fragment, props in paragraphs:
        heading = _heading(fragment, props, default_size)
        indent = props.get("IndentLevel", 1)
        if (
            current
            and any(f.text.strip() for f in current)
            and (heading or not section and fragment.text.strip() and indent <= root_indent)
        ):
            groups.append(current)
            current = []
        if not current or not any(f.text.strip() for f in current):
            section = heading
            root_indent = indent
        current.append(fragment)
    if current:
        groups.append(current)
    # A trailing heading is kept with its predecessor; it never forms an orphan page.
    if len(groups) > 1:
        trailing_heading = len(groups[-1]) == 1 and _heading(groups[-1][0], paragraphs[-1][1], default_size)
        if trailing_heading or all(not f.text.strip() for f in groups[-1]):
            groups[-2].extend(groups.pop())
    return [Unit(f"{shape.shape_id}:{index}", tuple(group)) for index, group in enumerate(groups)]


def build_draft(deck, findings, policy, slide):
    if policy.strength == Strength.LIGHT:
        raise SplitUnavailable(
            "가벼운 수정에서는 배치를 바꾸지 않습니다. 표준 이상을 선택하면 분할안을 검토할 수 있습니다."
        )
    related = [f for f in findings if f.shape_key.split(":", 1)[0] == str(slide)]
    if not any(
        f.rule_id == "layout.density" or f.rule_id == "typography" and not f.id.endswith(":hierarchy")
        for f in related
    ):
        raise SplitUnavailable("본문의 정보량 또는 글자 크기 문제가 없는 슬라이드입니다.")
    shapes = [
        s
        for s in deck.shapes
        if s.slide_number == slide
        and (s.locator is None or s.locator.kind == "shape" and len(s.locator.shape_path) == 1)
        and s.effective_visible is not False
    ]
    if not shapes or any(s.box is None for s in shapes):
        raise SplitUnavailable("일부 객체의 위치를 확인할 수 없습니다.")
    if any(f"{slide}:" == key[: len(str(slide)) + 1] for key in policy.locked_shapes):
        raise SplitUnavailable("이 슬라이드에 고정된 객체가 있어 본문을 재구성하지 않았습니다.")
    if policy.locked_properties & {"font_size", "position", "size"}:
        raise SplitUnavailable("글자 크기·위치·도형 크기 고정을 해제해야 분할할 수 있습니다.")
    if any(s.shape_type in (7, 9, 10, 11, 16) for s in shapes):
        raise SplitUnavailable("연결선이나 포함·외부 연결 객체의 내용을 검증할 수 없어 분할하지 않았습니다.")
    manual_numbers = {
        s.shape_id
        for s in shapes
        if re.fullmatch(rf"\s*{slide}\s*/\s*{deck.slide_count}\s*", s.text)
        and (s.box.y <= deck.height_pt * 0.15 or s.box.y >= deck.height_pt * 0.85)
    }
    titles = [s for s in shapes if s.placeholder_type in TITLE_PLACEHOLDERS and s.text.strip()]
    texts = [
        s
        for s in shapes
        if s.text.strip() and s.placeholder_type not in FOOTERS and s.shape_id not in manual_numbers
    ]
    if not titles and texts:
        first = min(texts, key=lambda s: (s.box.y, s.box.x, s.shape_id))
        heading_size = max((run.font_size_pt or 0 for run in first.runs), default=0)
        other_sizes = [
            run.font_size_pt
            for shape in texts
            if shape is not first
            for run in shape.runs
            if run.font_size_pt and run.text.strip()
        ]
        if (
            first.box.y <= deck.height_pt * 0.25
            and len(first.text.strip()) <= 100
            and not re.search(r"[\r\n]", first.text)
            and other_sizes
            and heading_size >= max(other_sizes) * 1.25
            and all(not dict(p.properties).get("bullet", True) for p in first.spans if p.kind == "paragraph")
        ):
            titles = [first]
    if len(titles) != 1:
        raise SplitUnavailable("제목과 본문을 하나의 읽기 순서로 구분하지 못했습니다.")
    title_ids = {s.shape_id for s in titles}
    footers = {s.shape_id for s in shapes if s.placeholder_type in FOOTERS} | manual_numbers
    repeated = title_ids | footers
    for shape in shapes:
        if not shape.text.strip() and (
            shape.box.width >= deck.width_pt * 0.9 or shape.box.height >= deck.height_pt * 0.9
        ):
            repeated.add(shape.shape_id)
    bodies = sorted(
        (s for s in texts if s.shape_id not in repeated), key=lambda s: (s.box.y, s.box.x, s.shape_id)
    )
    if not bodies:
        raise SplitUnavailable("분할할 일반 본문을 찾지 못했습니다.")
    for index, a in enumerate(bodies):
        for b in bodies[index + 1 :]:
            if min(a.box.y + a.box.height, b.box.y + b.box.height) - max(a.box.y, b.box.y) > 2:
                raise SplitUnavailable("다단 또는 서로 겹친 본문의 읽기 순서를 확인할 수 없습니다.")
    default_size = policy.font_limits.recommended_pt * slide_scale(deck.height_pt)
    units = []
    atomic = set()
    for shape in bodies:
        if not shape.supported or re.search(r"[=∑∫√]", shape.text):
            atomic.add(shape.shape_id)
    body_ids = {s.shape_id for s in bodies}
    other_objects = [s for s in shapes if s.shape_id not in body_ids | repeated]
    linked = {s.shape_id: [] for s in bodies}
    for obj in other_objects:
        # Keep a picture/table/equation and its closest text box as one indivisible unit.
        ranked = sorted(
            bodies,
            key=lambda s: (
                abs((s.box.y + s.box.height / 2) - (obj.box.y + obj.box.height / 2))
                + abs((s.box.x + s.box.width / 2) - (obj.box.x + obj.box.width / 2)),
                s.shape_id,
            ),
        )
        captions = [
            s for s in ranked if re.match(r"\s*(그림\s*\d|표\s*\d|Fig(?:ure)?\.?\s*\d)", s.text, re.I)
        ]
        anchor = captions[0] if captions else ranked[0]
        overlapping = [
            s
            for s in bodies
            if min(s.box.y + s.box.height, obj.box.y + obj.box.height) > max(s.box.y, obj.box.y)
            and min(s.box.x + s.box.width, obj.box.x + obj.box.width) > max(s.box.x, obj.box.x)
        ]
        if len(overlapping) > 1:
            raise SplitUnavailable("그림·표가 여러 본문과 연결되어 있어 하나의 묶음을 확인할 수 없습니다.")
        if overlapping:
            anchor = overlapping[0]
        linked[anchor.shape_id].append(obj.shape_id)
        atomic.add(anchor.shape_id)
    for shape in bodies:
        if shape.shape_id in atomic:
            value = canonical_text(shape.text)
            units.append(
                Unit(
                    f"{shape.shape_id}:whole",
                    (Fragment(shape.shape_id, 0, len(value), value, True),),
                    tuple(sorted(linked[shape.shape_id])),
                )
            )
        else:
            units.extend(_units(shape, default_size))
    if not 2 <= len(units) <= MAX_UNITS:
        raise SplitUnavailable(
            "나눌 수 있는 문단 묶음이 부족하거나 너무 많습니다. 표·수식·관련 그림 묶음은 쪼개지 않습니다."
        )
    margin_x, margin_y = deck.width_pt * policy.margin_ratio, deck.height_pt * policy.margin_ratio
    gap = round(max(8.0, default_size * 0.4), 2)
    top = max(margin_y, titles[0].box.y + titles[0].box.height + gap)
    bottom = deck.height_pt - margin_y
    for shape in shapes:
        if shape.shape_id in footers and shape.box.y > deck.height_pt * 0.5:
            bottom = min(bottom, shape.box.y - gap)
    if bottom <= top + default_size * 2:
        raise SplitUnavailable("제목과 바닥글 사이에 본문을 배치할 공간이 부족합니다.")
    return SplitDraft(
        slide,
        bodies[0].shape_id,
        tuple(sorted(title_ids)),
        tuple(sorted(repeated)),
        tuple(units),
        round(default_size, 2),
        policy.fix.line_spacing or 1.2,
        policy.margin_ratio,
        round(top, 2),
        round(bottom, 2),
        round(deck.width_pt - 2 * margin_x, 2),
        round(margin_x, 2),
        gap,
        tuple((s.shape_id, canonical_text(s.text)) for s in bodies),
        tuple(
            sorted(
                s.shape_id
                for s in deck.shapes
                if s.slide_number == slide
                and s.effective_visible is False
                and (s.locator is None or s.locator.kind == "shape" and len(s.locator.shape_path) == 1)
            )
        ),
    )


def partition(heights, capacity, gap, *, max_pages=MAX_PAGES):
    """Minimum pages, then minimum squared unused space; stable ties preserve order."""
    if not heights or len(heights) > MAX_UNITS or any(h <= 0 or not math_isfinite(h) for h in heights):
        raise SplitUnavailable("본문 높이를 확인할 수 없습니다.")
    if any(h > capacity + 0.1 for h in heights):
        raise SplitUnavailable("분리할 수 없는 소제목·하위 항목 또는 그림 묶음 하나가 한 장보다 큽니다.")

    @lru_cache(None)
    def solve(start):
        if start == len(heights):
            return 0, 0.0, ()
        used, best = 0.0, None
        for end in range(start + 1, len(heights) + 1):
            used += heights[end - 1] + (gap if end - start > 1 else 0)
            if used > capacity + 0.1:
                break
            pages, waste, cuts = solve(end)
            candidate = pages + 1, round(waste + (capacity - used) ** 2, 4), (end, *cuts)
            if best is None or candidate < best:
                best = candidate
        return best

    pages, _, cuts = solve(0)
    if pages > max_pages:
        raise SplitUnavailable(f"분할 결과가 {max_pages}장을 초과합니다.")
    return cuts


def math_isfinite(value):
    import math

    return isinstance(value, (float, int)) and math.isfinite(value)


def plan_digest(payload):
    data = {k: v for k, v in payload.items() if k not in ("validated", "digest")}
    return sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_manifest(payload):
    """Independent coverage check: partitioned pages must consume every source range."""
    units, cuts = payload.get("units", ()), payload.get("cuts", ())
    if not 2 <= len(cuts) <= MAX_PAGES or not units or cuts[-1] != len(units):
        raise SplitUnavailable("분할 계획이 본문 전체를 포함하지 않습니다.")
    if any(type(cut) is not int for cut in cuts) or list(cuts) != sorted(set(cuts)) or cuts[0] <= 0:
        raise SplitUnavailable("분할 경계가 원문 순서와 일치하지 않습니다.")
    source = dict(payload["source_text"])
    if len(source) != len(payload["source_text"]):
        raise SplitUnavailable("원본 객체가 분할 계획에 중복되었습니다.")
    offsets = {shape_id: 0 for shape_id in source}
    objects = []
    order = []
    for unit in units:
        objects.extend(unit["object_ids"])
        for fragment in unit["fragments"]:
            shape_id, start, end = fragment["shape_id"], fragment["start"], fragment["end"]
            if not order or order[-1] != shape_id:
                order.append(shape_id)
            if (
                shape_id not in source
                or start != offsets[shape_id]
                or not start < end <= len(source[shape_id])
            ):
                raise SplitUnavailable("본문 범위가 누락·중복되거나 순서가 바뀌었습니다.")
            if source[shape_id][start:end] != fragment["text"]:
                raise SplitUnavailable("계획의 문장이 원문과 일치하지 않습니다.")
            offsets[shape_id] = end
    if any(offsets[shape_id] != len(value) for shape_id, value in source.items()):
        raise SplitUnavailable("분할 계획에서 누락된 본문이 있습니다.")
    if order != list(source):
        raise SplitUnavailable("본문 객체의 원래 읽기 순서가 바뀌었습니다.")
    repeated = set(payload["repeated_ids"]) | set(payload.get("once_ids", ()))
    if set(source) & repeated or len(objects) != len(set(objects)) or set(objects) & (set(source) | repeated):
        raise SplitUnavailable("그림·표 묶음이 중복 배치되었습니다.")


def output_slide_numbers(proposals, slide):
    splits = [p for p in proposals if p.property == "slide.split"]
    offset = sum(int(p.after) - 1 for p in splits if int(p.shape_key.split(":")[0]) < slide)
    count = next((int(p.after) for p in splits if int(p.shape_key.split(":")[0]) == slide), 1)
    return tuple(range(slide + offset, slide + offset + count))
