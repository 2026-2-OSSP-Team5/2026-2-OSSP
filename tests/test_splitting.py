from dataclasses import replace

import pytest

from slideguard.domain.models import Box, DeckSnapshot, Finding, ObjectLocator, Shape, TextRun, TextSpan
from slideguard.domain.policy import AnalysisPolicy, Strength
from slideguard.planning.splitting import (
    SplitUnavailable,
    build_draft,
    canonical_text,
    output_slide_numbers,
    partition,
    verify_manifest,
)


def _deck(paragraphs, *, picture=False):
    title = Shape(2, 2, "제목", Box(50, 28, 800, 50), "제목", supported=True, placeholder_type=1)
    text = "\r".join(value for value, _ in paragraphs)
    spans = []
    for index, (value, props) in enumerate(paragraphs, 1):
        spans.append(
            TextSpan(
                "paragraph",
                index,
                value + ("\r" if index < len(paragraphs) else ""),
                properties=tuple(props.items()),
            )
        )
    body = Shape(
        2,
        3,
        "본문",
        Box(50, 100, 800, 350),
        text,
        (TextRun(text, 12),),
        supported=True,
        spans=tuple(spans),
        shape_type=17,
        locator=ObjectLocator((3,)),
    )
    shapes = (title, body)
    if picture:
        shapes += (Shape(2, 4, "관련 그림", Box(550, 250, 250, 150), shape_type=13),)
    return DeckSnapshot("deck.pptx", "hash", 2, 960, 540, shapes)


def _findings():
    return [
        Finding("layout.density:2:chars", "layout.density", "2:3", "warning", "내용이 많음", 300, 100, "")
    ]


def test_paragraph_and_nested_list_units_preserve_original_order():
    deck = _deck(
        [
            ("일반 문단", {}),
            ("목록 부모", {"bullet": True, "IndentLevel": 1}),
            ("하위 항목", {"bullet": True, "IndentLevel": 2}),
            ("다음 목록", {"bullet": True, "IndentLevel": 1}),
        ]
    )
    draft = build_draft(deck, _findings(), AnalysisPolicy(), 2)
    assert [len(unit.fragments) for unit in draft.units] == [1, 2, 1]
    assert "".join(fragment.text for unit in draft.units for fragment in unit.fragments) == canonical_text(
        deck.shapes[1].text
    )
    assert draft.payload() == build_draft(deck, _findings(), AnalysisPolicy(), 2).payload()


def test_subheading_and_following_section_are_indivisible():
    deck = _deck(
        [
            ("첫 소제목:", {}),
            ("첫 설명", {}),
            ("첫 항목", {"bullet": True}),
            ("두 번째 소제목:", {}),
            ("두 번째 설명", {}),
        ]
    )
    draft = build_draft(deck, _findings(), AnalysisPolicy(), 2)
    assert [len(unit.fragments) for unit in draft.units] == [3, 2]
    trailing = _deck([("첫 문단", {}), ("다음 문단", {}), ("마지막 소제목:", {})])
    assert [
        len(unit.fragments) for unit in build_draft(trailing, _findings(), AnalysisPolicy(), 2).units
    ] == [1, 2]


def test_picture_and_its_body_are_not_split_or_silently_omitted():
    deck = _deck([("첫 문단", {}), ("둘째 문단", {})], picture=True)
    with pytest.raises(SplitUnavailable, match="묶음"):
        build_draft(deck, _findings(), AnalysisPolicy(), 2)


def test_incomplete_paragraphs_and_locks_are_rejected():
    deck = _deck([("첫 문단", {}), ("둘째 문단", {})])
    with pytest.raises(SplitUnavailable, match="고정"):
        build_draft(deck, _findings(), AnalysisPolicy(locked_shapes=frozenset({"2:3"})), 2)
    with pytest.raises(SplitUnavailable, match="가벼운"):
        build_draft(deck, _findings(), AnalysisPolicy(strength=Strength.LIGHT), 2)
    body = replace(deck.shapes[1], spans=deck.shapes[1].spans[:1])
    with pytest.raises(SplitUnavailable, match="전체"):
        build_draft(replace(deck, shapes=(deck.shapes[0], body)), _findings(), AnalysisPolicy(), 2)


def test_partition_minimizes_pages_then_balances_with_stable_ties():
    assert partition([100, 100, 100, 100, 100], 350, 10) == (2, 5)
    assert partition([100, 100], 220, 10) == (2,)
    with pytest.raises(SplitUnavailable, match="한 장보다"):
        partition([500, 100], 400, 8)
    with pytest.raises(SplitUnavailable):
        partition([float("nan")], 400, 8)


def test_source_slide_numbers_shift_once_for_each_previous_split():
    from slideguard.domain.models import ChangeProposal

    plans = [
        ChangeProposal("a", "f", "2:3", "slide.split", 1, 3, 0.5),
        ChangeProposal("b", "f", "5:3", "slide.split", 1, 2, 0.5),
    ]
    assert output_slide_numbers(plans, 2) == (2, 3, 4)
    assert output_slide_numbers(plans, 5) == (7, 8)
    assert output_slide_numbers(plans, 7) == (10,)


def test_independent_manifest_rejects_missing_duplicate_reordered_or_rewritten_body():
    import copy

    deck = _deck([("첫 문단", {}), ("둘째 문단", {}), ("셋째 문단", {})])
    payload = build_draft(deck, _findings(), AnalysisPolicy(), 2).payload()
    payload["cuts"] = [1, 3]
    verify_manifest(payload)
    for change in ("missing", "duplicate", "order", "rewrite", "picture", "repeated"):
        invalid = copy.deepcopy(payload)
        if change == "missing":
            invalid["units"] = invalid["units"][:-1]
        elif change == "duplicate":
            invalid["units"] = (invalid["units"][0], invalid["units"][0], invalid["units"][2])
        elif change == "order":
            invalid["units"] = tuple(reversed(invalid["units"]))
        elif change == "rewrite":
            invalid["units"][0]["fragments"][0]["text"] = "변경된 문장"
        elif change == "picture":
            invalid["units"][0]["object_ids"] = (9,)
            invalid["units"][1]["object_ids"] = (9,)
        else:
            invalid["repeated_ids"] = (3,)
        with pytest.raises(SplitUnavailable):
            verify_manifest(invalid)


def test_hidden_objects_are_preserved_once_and_numbered_lists_require_known_numbers():
    deck = _deck(
        [
            ("첫 항목", {"bullet": True, "BulletType": 2, "BulletNumber": 7}),
            ("둘째 항목", {"bullet": True, "BulletType": 2, "BulletNumber": 8}),
        ]
    )
    hidden = Shape(2, 9, "숨긴 객체", Box(0, 0, 10, 10), "숨긴 원문", effective_visible=False)
    draft = build_draft(replace(deck, shapes=(*deck.shapes, hidden)), _findings(), AnalysisPolicy(), 2)
    assert draft.once_ids == (9,)
    assert dict(draft.units[0].fragments[0].properties)["BulletNumber"] == 7
    unknown = _deck([("첫 항목", {"bullet": True, "BulletType": 2}), ("둘째 항목", {})])
    with pytest.raises(SplitUnavailable, match="번호"):
        build_draft(unknown, _findings(), AnalysisPolicy(), 2)


def test_ordinary_first_paragraph_cannot_be_repeated_as_an_inferred_title():
    deck = _deck([("첫 문단", {}), ("둘째 문단", {})])
    title = replace(deck.shapes[0], placeholder_type=None, runs=(TextRun("제목처럼 보이는 본문", 12),))
    with pytest.raises(SplitUnavailable, match="제목"):
        build_draft(replace(deck, shapes=(title, deck.shapes[1])), _findings(), AnalysisPolicy(), 2)


def test_leading_blank_paragraph_stays_with_content_without_an_empty_page_unit():
    deck = _deck([("", {}), ("첫 문단", {}), ("둘째 문단", {})])
    draft = build_draft(deck, _findings(), AnalysisPolicy(), 2)
    assert len(draft.units) == 2
    assert "".join(f.text for f in draft.units[0].fragments) == "\r첫 문단\r"
