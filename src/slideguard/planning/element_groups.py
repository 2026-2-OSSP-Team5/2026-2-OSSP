"""Document-wide element identity, proposal views and explicit position alignment."""

import math
import re
from collections import Counter
from dataclasses import replace

from slideguard.domain.models import ChangeProposal, ElementGroup
from slideguard.domain.policy import BODY_PLACEHOLDERS, Strength
from slideguard.planning.bindings import is_recommended


def eligible_shapes(deck):
    hidden = {s.number for s in deck.slides if s.hidden is True}
    return {
        s.key: s
        for s in deck.shapes
        if s.box is not None
        and s.supported
        and s.effective_visible is not False
        and s.slide_number not in hidden
        and s.text.strip()
        and len(s.key.split(":")) == 2
    }


def identity(shape):
    """Keep text identity while allowing page numbers to change after reordering."""
    text = re.sub(r"\s+", " ", shape.text.strip()).casefold()
    if re.fullmatch(r"[\d\s./()\-–]+", text):
        text = "<page-number>"
    return f"{shape.placeholder_type}:{text}"


def _same_element(a, b, deck):
    if a.slide_number == b.slide_number or a.placeholder_type != b.placeholder_type:
        return False
    if a.placeholder_type in BODY_PLACEHOLDERS:
        return False
    x, y = a.box, b.box
    same_box = all(
        abs(v - w) <= 1
        for v, w in zip((x.x, x.y, x.width, x.height), (y.x, y.y, y.width, y.height), strict=True)
    )
    # Exact placement alone must not group unrelated full-size body text boxes.
    small = max(x.height, y.height) <= deck.height_pt * 0.15
    if same_box and small:
        return True
    if identity(a) != identity(b) or not small:
        return False
    sizes_match = abs(x.width - y.width) <= max(4, max(x.width, y.width) * 0.2)
    sizes_match &= abs(x.height - y.height) <= max(4, max(x.height, y.height) * 0.2)
    nearby = abs(x.x - y.x) <= deck.width_pt * 0.15 and abs(x.y - y.y) <= deck.height_pt * 0.15
    return sizes_match and nearby


def detect_groups(deck, saved=()):
    shapes = eligible_shapes(deck)
    result, reserved = [], set()
    for group in saved:
        members = tuple(k for k in group.members if k in shapes and k not in reserved)
        reserved.update(members)
        reserved.update(group.excluded)
        if members:
            result.append(replace(group, members=members))
    remaining = [
        s
        for s in shapes.values()
        if s.key not in reserved
        and s.placeholder_type not in BODY_PLACEHOLDERS
        and s.box.height <= deck.height_pt * 0.15
    ]
    used = set()
    for anchor in remaining:
        if anchor.key in used:
            continue
        members = [anchor.key]
        slides = {anchor.slide_number}
        for other in remaining:
            if other.key in used or other.slide_number in slides:
                continue
            if _same_element(anchor, other, deck):
                members.append(other.key)
                slides.add(other.slide_number)
        if len(members) < 2:
            continue
        used.update(members)
        title = (
            "쪽 번호" if identity(anchor).endswith("<page-number>") else " ".join(anchor.text.split())[:32]
        )
        result.append(ElementGroup(f"element:{anchor.key}", title or anchor.name, tuple(members)))
    return result


def ensure_groups(report):
    if not report.element_groups and getattr(report, "_element_groups_deck", None) is not report.deck:
        report.element_groups = detect_groups(report.deck)
    report._element_groups_deck = report.deck
    return report.element_groups


def edit_group(groups, group):
    """A shape belongs to at most one group; exclusions survive another analysis."""
    old = next((g for g in groups if g.id == group.id), None)
    excluded = set(group.excluded)
    if old:
        excluded.update(set(old.members) - set(group.members))
        excluded.update(old.excluded)
    excluded.difference_update(group.members)
    edited = replace(group, manual=True, excluded=tuple(sorted(excluded)))
    result = []
    for other in groups:
        if other.id == group.id:
            continue
        members = tuple(k for k in other.members if k not in group.members)
        if members:
            result.append(replace(other, members=members))
    return [*result, edited]


def proposal_options(report, keys):
    """Aggregate existing, validated changes; never copy colors between backgrounds.

    Atomic reflow bundles stay intact, including their dependent objects.
    Returns (ids, representative, slide_count, differing_values).
    """
    keys = set(keys)
    bundles = {}
    for p in report.proposals:
        token = p.id.split("#", 1)[1] if "#" in p.id else p.id
        bundles.setdefault(token, []).append(p)
    actions = {}
    for bundle in bundles.values():
        own = [p for p in bundle if p.shape_key in keys and p.property != "slide.split"]
        if not own:
            continue
        signature = (
            is_recommended(own[0].id),
            "#align:" in own[0].id,
            tuple(sorted({p.property for p in own})),
        )
        actions.setdefault(signature, []).append((bundle, own))
    result = []
    for entries in actions.values():
        own = [p for _, items in entries for p in items]
        ids = tuple(dict.fromkeys(p.id for bundle, _ in entries for p in bundle))
        count = len({p.shape_key.split(":", 1)[0] for p in own})
        values = {(p.property, repr(p.before), repr(p.after)) for p in own}
        result.append((ids, own[0], count, len(values) > len({p.property for p in own})))
    return result


def group_problem_text(report, group):
    counts = Counter()
    for f in report.findings:
        if f.shape_key in group.members and f.rule_id != "layout.density":
            counts[f.rule_id] += 1
    return counts


def grouped_findings(report):
    """One global row per element group; keep slide-wide issues as their own rows."""
    by_member = {key: g for g in report.element_groups for key in g.members}
    buckets = {}
    for index, finding in enumerate(report.findings):
        group = by_member.get(finding.shape_key) if finding.rule_id != "layout.density" else None
        token = group.id if group else index
        buckets.setdefault(token, (group, []))[1].append(finding)
    rank = {"warning": 0, "review": 1, "unknown": 2}
    return [(min(items, key=lambda f: rank[f.severity]), group, items) for group, items in buckets.values()]


def align_group(deck, group, reference_key, policy):
    """Explicit alignment proposals, validated as one movement of the chosen group."""
    shapes = eligible_shapes(deck)
    if reference_key not in group.members or reference_key not in shapes:
        raise ValueError("위치 기준으로 사용할 그룹 요소를 선택하세요.")
    if policy.strength == Strength.LIGHT:
        raise ValueError("위치를 맞추려면 수정 강도를 표준 이상으로 설정하세요.")
    anchor = shapes[reference_key].box
    candidates, rejected = [], []
    for key in group.members:
        shape = shapes.get(key)
        if shape is None:
            continue
        box = shape.box
        if math.hypot(box.x - anchor.x, box.y - anchor.y) <= 1:
            continue
        if not policy.allows_change(key, "position"):
            rejected.append(f"{shape.slide_number}쪽: 위치 고정 또는 검사 범위 밖")
            continue
        if (
            anchor.x < 0
            or anchor.y < 0
            or anchor.x + box.width > deck.width_pt
            or anchor.y + box.height > deck.height_pt
        ):
            rejected.append(f"{shape.slide_number}쪽: 슬라이드 밖으로 나감")
            continue

        def overlaps(x, y, other, box=box):
            b = other.box
            return (
                min(x + box.width, b.x + b.width) - max(x, b.x) > 1
                and min(y + box.height, b.y + b.height) - max(y, b.y) > 1
            )

        blocked = False
        for other in deck.shapes:
            if (
                other.key == key
                or other.slide_number != shape.slide_number
                or other.box is None
                or other.effective_visible is False
            ):
                continue
            if overlaps(anchor.x, anchor.y, other) and not overlaps(box.x, box.y, other):
                blocked = True
                break
            holder = other.box
            contained_before = (
                holder.x <= box.x
                and holder.y <= box.y
                and holder.x + holder.width >= box.x + box.width
                and holder.y + holder.height >= box.y + box.height
            )
            contained_after = (
                holder.x <= anchor.x
                and holder.y <= anchor.y
                and holder.x + holder.width >= anchor.x + box.width
                and holder.y + holder.height >= anchor.y + box.height
            )
            if contained_before and not contained_after:
                blocked = True
                break
        if blocked:
            rejected.append(f"{shape.slide_number}쪽: 다른 요소와 새로 겹치거나 배경 도형 밖으로 나감")
            continue
        candidates.append(
            ChangeProposal(
                f"shape.position@{key}#align:{group.id}",
                f"group.alignment:{group.id}:{key}",
                key,
                "shape.position",
                (box.x, box.y),
                (anchor.x, anchor.y),
                math.hypot(box.x - anchor.x, box.y - anchor.y) / deck.height_pt,
                explanation=f"{group.title}: {shapes[reference_key].slide_number}쪽 요소의 위치에 맞춤",
            )
        )
    return candidates, rejected
