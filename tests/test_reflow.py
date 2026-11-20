"""슬라이드 재배치(planning/reflow.py) 테스트: 글자를 키우거나 넘친 상자를 늘릴 때 슬라이드 전체를 다시 쌓는다."""

from slideguard.domain.models import AnalysisReport, Box, DeckSnapshot, Finding, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy, Strength
from slideguard.planning import MinimumChangePlanner, Property, RuleBasedPlanner
from slideguard.planning.bindings import is_recommended
from slideguard.planning.reflow import FlowItem, predicted_height, reflow_slide, wrapped_lines

SLIDE_H = 540.0


def text(shape_id, top, height, *, text_height, left=50.0, width=400.0, size=12.0, body="가" * 60, z=None):
    return FlowItem(
        shape_id,
        left,
        top,
        width,
        height,
        is_text=True,
        text=body,
        size_pt=size,
        text_height=text_height,
        margins=7.2,
        inner_width=width - 14.4,
        z_order=z,
    )


def run(items, sizes=None, grow=None, fit=()):
    return reflow_slide(items, sizes or {}, grow or {}, set(fit), slide_h=SLIDE_H, bottom_margin=27.0)


# ---------------------------------------------------------------- 높이 예상


def test_wrapped_lines_counts_korean_wider_than_latin():
    assert wrapped_lines("가" * 20, 10, 100) == 2  # 한글 20자 × 10pt = 200pt → 100pt 폭에 2줄
    assert wrapped_lines("a" * 20, 10, 100) == 2  # 영문 20자 × 5.5pt = 110pt → 2줄
    assert wrapped_lines("a" * 18, 10, 100) == 1
    assert wrapped_lines("첫 줄\n둘째 줄", 10, 1000) == 2


def test_small_shortfall_does_not_move_the_slide():
    a = text(1, 100, 104.2, text_height=96.0)  # 96 + 7.2 + 2 = 105.2 → 1pt 모자람
    b = text(2, 230, 50, text_height=30)
    flow = run([a, b], fit={1})
    assert flow.ok and not flow.changes


def test_bigger_font_needs_more_lines_and_taller_lines():
    item = text(1, 100, 60, text_height=40, size=12)
    assert predicted_height(item, None) == 40 + 7.2 + 2.0  # 크기를 안 바꾸면 지금 높이
    bigger = predicted_height(item, 18)
    assert bigger > 40 * 1.5  # 줄이 굵어지고(×1.5) 줄 수도 늘어남


# ---------------------------------------------------------------- 쌓기


def test_growing_box_uses_free_space_first_then_pushes_the_rest():
    a = text(1, 100, 60, text_height=40)
    b = text(2, 180, 50, text_height=30)  # a와 간격 20
    c = text(3, 250, 50, text_height=30)  # b와 간격 20
    side = text(4, 100, 300, text_height=30, left=500, width=150)  # 옆 칸: 안 움직임
    flow = run([a, b, c, side], sizes={1: 18})
    grown = flow.heights[1] - 60
    assert flow.ok and grown > 0
    # a 아래 빈 공간 20pt 중 10pt는 a가 쓰고, 나머지만큼 b를 내림. b와 c 사이 간격은 그대로
    assert flow.tops[2] == round(180 + grown - 10, 2) and flow.tops[3] == round(250 + grown - 10, 2)
    assert 4 not in flow.tops


def test_panel_behind_text_grows_with_it():
    card = FlowItem(10, 40, 90, 420, 100, z_order=1)  # 글상자 뒤 카드 배경
    a = text(1, 100, 60, text_height=40, z=2)
    flow = run([card, a], sizes={1: 18})
    assert flow.ok and flow.heights[10] - 100 == flow.heights[1] - 60
    assert 10 not in flow.tops


def test_text_boxes_that_already_collide_are_separated():
    a = text(1, 100, 60, text_height=80)  # 넘쳐서 아래 상자 위까지 글자가 내려옴
    b = text(2, 150, 50, text_height=30)  # a와 원래 10pt 겹침
    flow = run([a, b], fit={1})
    assert flow.ok and flow.tops[2] >= 100 + flow.heights[1] + 6  # 늘어난 a 아래로 6pt 띄움


def test_does_not_fit_on_slide_gives_a_reason_instead():
    a = text(1, 100, 300, text_height=290, body="가" * 400)
    b = text(2, 420, 60, text_height=40)
    flow = run([a, b], sizes={1: 24})
    assert not flow.ok and "슬라이드 나누기" in flow.reason and not flow.changes


def test_fixed_footer_is_never_pushed_into():
    a = text(1, 340, 100, text_height=125)  # 넘침: 135pt 필요 → 아래 끝 475
    footer = FlowItem(9, 50, 470, 400, 30, fixed=True)
    flow = run([a, footer], fit={1})
    assert not flow.ok and "고정 객체" in flow.reason


# ---------------------------------------------------------------- 수정 후보와 연결


def _shape(shape_id, top, height, text_height, size=12.0, body="가" * 60):
    box = Box(50, top, 400, height)
    return Shape(
        slide_number=1,
        shape_id=shape_id,
        name=f"글상자 {shape_id}",
        box=box,
        text=body,
        runs=(TextRun(body, font_size_pt=size, color=(0, 0, 0)),),
        background=(255, 255, 255),
        supported=True,
        rendered_box=Box(57.2, top + 3.6, 385.6, text_height),
        content_box=Box(57.2, top + 3.6, 385.6, height - 7.2),
        z_order=shape_id,
    )


def _report():
    deck = DeckSnapshot(
        "deck.pptx", "abc", 1, 720.0, 540.0, shapes=(_shape(2, 100, 60, 45), _shape(3, 180, 60, 30))
    )
    small = Finding("typography:1:2:1", "typography", "1:2", "warning", "작음", 12.0, 14.0, "")
    return AnalysisReport(deck=deck, findings=[small])


def test_font_growth_becomes_one_bundle_with_box_and_moves():
    report = _report()
    result = RuleBasedPlanner().plan(report.deck, report.findings, AnalysisPolicy())
    bundle = [p for p in result.proposals if p.group == "reflow:1"]
    assert {(p.prop, p.shape_id) for p in bundle} == {
        (Property.FONT_SIZE, 2),
        (Property.SHAPE_HEIGHT, 2),
        (Property.SHAPE_POSITION, 3),
    }
    move = next(p for p in bundle if p.prop == Property.SHAPE_POSITION)
    height = next(p for p in bundle if p.prop == Property.SHAPE_HEIGHT)
    assert move.after[1] - 180 == height.after - 60 - 10  # 빈 공간 20pt 중 10pt를 쓰고 나머지만큼 내림
    assert all(p.finding_id == "typography:1:2:1" for p in bundle)


def test_recommended_variant_gets_its_own_reflow_bundle():
    report = _report()
    proposals = MinimumChangePlanner().propose(report, AnalysisPolicy())
    ids = [p.id for p in proposals]
    assert len(ids) == len(set(ids))
    rec = [p for p in proposals if is_recommended(p.id)]
    assert {p.property for p in rec} == {"font.size", "shape.height", "shape.position"}
    assert all(p.id.endswith("#reflow:1~rec") for p in rec)  # 권장 묶음은 따로 켜고 끔


def test_passive_strength_does_not_move_objects():
    report = _report()
    result = RuleBasedPlanner().plan(report.deck, report.findings, AnalysisPolicy(strength=Strength.LIGHT))
    assert {p.prop for p in result.proposals} == {Property.FONT_SIZE}
