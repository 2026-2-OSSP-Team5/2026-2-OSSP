"""숨긴 객체는 검사 대상에서 빠지고, 숨김 여부를 모르면 검사한다."""

import pytest

from slideguard.checks.contrast import ContrastRule
from slideguard.checks.layout import LayoutRule
from slideguard.checks.scope import target_shapes
from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, SlideDetails, TextRun
from slideguard.domain.policy import AnalysisPolicy

GRAY_777 = (119, 119, 119)  # 흰 배경 대비 약 4.48:1
WHITE = (255, 255, 255)

RULES = (ContrastRule(), TypographyRule(), LayoutRule())


def make_shape(visible=None, slide_number=1, shape_id=1, x=-10, size=9, text="본문"):
    """기본값은 여백 위반·작은 글씨·낮은 명암비를 동시에 가진 도형이다."""
    return Shape(
        slide_number=slide_number,
        shape_id=shape_id,
        name=f"shape{shape_id}",
        box=Box(x, 100, 200, 50),
        text=text,
        runs=(TextRun(text, size, GRAY_777, False),) if text else (),
        background=WHITE,
        supported=True,
        effective_visible=visible,
    )


def make_deck(*shapes, slides=()):
    return DeckSnapshot(
        source_path="test.pptx",
        source_sha256="0" * 64,
        slide_count=3,
        width_pt=960,
        height_pt=540,
        shapes=tuple(shapes),
        slides=tuple(slides),
    )


def all_findings(deck, policy=None):
    policy = policy or AnalysisPolicy()
    return [f for rule in RULES for f in rule.evaluate(deck, policy)]


# --- 도형 숨김 ---------------------------------------------------------------
def test_hidden_shape_produces_no_findings():
    assert all_findings(make_deck(make_shape(visible=False))) == []


def test_visible_shape_is_checked():
    assert all_findings(make_deck(make_shape(visible=True))) != []


def test_unknown_visibility_is_still_checked():
    """숨김 여부를 모르면 건너뛰지 않는다. 놓치는 쪽이 더 위험하다."""
    assert all_findings(make_deck(make_shape(visible=None))) != []


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.id)
def test_every_rule_skips_hidden_shapes(rule):
    deck = make_deck(make_shape(visible=False))
    assert rule.evaluate(deck, AnalysisPolicy()) == []


# --- 슬라이드 숨김 -----------------------------------------------------------
def test_hidden_slide_excludes_its_shapes():
    deck = make_deck(make_shape(slide_number=2), slides=(SlideDetails(2, hidden=True),))
    assert all_findings(deck) == []


def test_visible_slide_is_checked():
    deck = make_deck(make_shape(slide_number=2), slides=(SlideDetails(2, hidden=False),))
    assert all_findings(deck) != []


def test_unknown_slide_visibility_is_still_checked():
    deck = make_deck(make_shape(slide_number=2), slides=(SlideDetails(2, hidden=None),))
    assert all_findings(deck) != []


def test_empty_slide_details_checks_everything():
    """OOXML reader처럼 슬라이드 정보를 주지 않으면 전부 검사한다."""
    assert all_findings(make_deck(make_shape(slide_number=2))) != []


def test_hidden_slide_does_not_affect_other_slides():
    deck = make_deck(
        make_shape(slide_number=1, shape_id=1),
        make_shape(slide_number=2, shape_id=2),
        slides=(SlideDetails(1, hidden=True),),
    )
    assert {f.shape_key for f in all_findings(deck)} == {"2:2"}


# --- 겹침·밀도에서의 숨김 ----------------------------------------------------
def test_hidden_shape_is_not_an_overlap_partner():
    visible = make_shape(visible=True, shape_id=1, x=100)
    hidden = make_shape(visible=False, shape_id=2, x=150)
    overlaps = [f for f in all_findings(make_deck(visible, hidden)) if f.rule_id == "layout.overlap"]
    assert overlaps == []


def test_visible_shapes_still_report_overlap():
    a = make_shape(visible=True, shape_id=1, x=100)
    b = make_shape(visible=True, shape_id=2, x=150)
    overlaps = [f for f in all_findings(make_deck(a, b)) if f.rule_id == "layout.overlap"]
    assert len(overlaps) == 1


def test_hidden_text_is_not_counted_in_density():
    hidden = make_shape(visible=False, shape_id=1, x=100, text="가" * 400)
    densities = [f for f in all_findings(make_deck(hidden)) if f.rule_id == "layout.density"]
    assert densities == []


# --- target_shapes 자체 -------------------------------------------------------
def test_target_shapes_keeps_policy_slide_filter():
    deck = make_deck(make_shape(slide_number=1, shape_id=1), make_shape(slide_number=3, shape_id=2))
    kept = target_shapes(deck, AnalysisPolicy(slides=(3,)))
    assert [s.key for s in kept] == ["3:2"]


def test_target_shapes_combines_hidden_and_policy_filters():
    deck = make_deck(
        make_shape(slide_number=3, shape_id=1, visible=False),
        make_shape(slide_number=3, shape_id=2, visible=True),
        make_shape(slide_number=1, shape_id=3, visible=True),
        slides=(SlideDetails(3, hidden=False),),
    )
    kept = target_shapes(deck, AnalysisPolicy(slides=(3,)))
    assert [s.key for s in kept] == ["3:2"]
