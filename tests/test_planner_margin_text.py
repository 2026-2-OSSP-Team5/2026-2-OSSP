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


def test_title_in_a_header_bar_is_only_indented_not_pushed_out_of_the_bar():
    """머리 띠 안의 제목이 위·왼쪽 여백을 모두 침범: 아래로 내리면 띠 밖이라 오른쪽 들여쓰기만 (자바게티 7·8쪽)."""
    bar = Shape(7, 1, "머리 띠", Box(0, 0, 1440, 77.5), supported=True)
    title = Shape(
        7,
        17,
        "제목",
        Box(21.3, 4.8, 648, 65.4),
        text="1. 개발 목적 및 필요성",
        runs=(TextRun("1. 개발 목적 및 필요성", 48.0, (255, 255, 255), False),),
        supported=True,
        rendered_box=Box(28.5, 8.4, 488.6, 57.6),
    )
    deck = DeckSnapshot("d.pptx", "0" * 64, 7, 1440, 810, shapes=(bar, title))
    finding = Finding(
        id="layout.margin:7:17",
        rule_id="layout.margin",
        shape_key="7:17",
        severity="review",
        message="텍스트가 안전 여백(5%) 안쪽에 있지 않습니다 (왼쪽)",
        measured=28.5,
        expected=72.0,
        evidence="",
        box=Box(28.5, 8.4, 488.6, 57.6),
    )
    result = RuleBasedPlanner().plan(deck, [finding], AnalysisPolicy())
    [move] = [p for p in result.proposals if p.prop == "shape.position"]
    assert move.after[1] == round(4.8, 2) and move.after[0] > 21.3  # 세로는 그대로, 가로만
    assert "가로만" in move.note
