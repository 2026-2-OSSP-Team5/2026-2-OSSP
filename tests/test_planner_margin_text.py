"""안전 여백 침범(검토) → 글자 영역을 가장 가까운 안쪽 경계로 옮기는 후보."""

from slideguard.domain.models import Box, DeckSnapshot, Finding, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning import RuleBasedPlanner


def test_subtitle_inside_a_wide_box_moves_just_enough_to_the_safe_area():
    # 1440pt 너비 슬라이드: 안전 여백 5% = 왼쪽 72pt. 글자는 55.2pt에서 시작 (상자는 더 넓음)
    shape = Shape(
        3,
        4,
        "소제목",
        Box(40, 40, 1300, 60),
        text="< 운영체제 >",
        runs=(TextRun("< 운영체제 >", 32.0, (0, 0, 0), False),),
        supported=True,
        rendered_box=Box(55.2, 48, 200, 44),
    )
    deck = DeckSnapshot("d.pptx", "0" * 64, 3, 1440, 810, shapes=(shape,))
    finding = Finding(
        id="layout.margin:3:4",
        rule_id="layout.margin",
        shape_key="3:4",
        severity="review",
        message="텍스트가 안전 여백(5%) 안쪽에 있지 않습니다 (왼쪽)",
        measured=55.2,
        expected=72.0,
        evidence="",
        box=Box(55.2, 48, 200, 44),
    )
    result = RuleBasedPlanner().plan(deck, [finding], AnalysisPolicy())
    [move] = [p for p in result.proposals if p.prop == "shape.position"]
    assert move.after == (round(40 + 72 - 55.2, 2), 40)
