"""색약 맞춤 검사와 수정 후보."""

from slideguard.checks.color_vision import ColorVisionRule, delta_e, simulate, worst_cvd_contrast
from slideguard.checks.contrast import contrast_ratio
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning import RuleBasedPlanner
from slideguard.planning.color import worst_vision_contrast

RED, BLACK, WHITE, GREEN = (0xE0, 0x30, 0x30), (0x20, 0x20, 0x20), (255, 255, 255), (0x20, 0x90, 0x30)


def _deck(*runs, background=BLACK):
    shape = Shape(
        1,
        2,
        "본문",
        Box(60, 100, 600, 80),
        text="".join(r.text for r in runs),
        runs=runs,
        background=background,
        supported=True,
    )
    return DeckSnapshot("d.pptx", "0" * 64, 1, 960, 540, shapes=(shape,))


def test_simulation_keeps_greys_and_darkens_red_for_protan():
    assert simulate(WHITE, "protan") == WHITE and simulate((0, 0, 0), "tritan") == (0, 0, 0)
    assert sum(simulate((255, 0, 0), "protan")) < 255  # 적색약에게 빨강은 어둡게 보임
    assert contrast_ratio(RED, BLACK) >= 3.0 > worst_cvd_contrast(RED, BLACK)[0]


def test_text_that_passes_for_normal_eyes_but_fails_for_protan_is_reviewed():
    """검은 배경 위 큰 빨간 글자: 보통 눈 3:1 통과, 적색약 눈 미달."""
    deck = _deck(TextRun("경고", 28.0, RED, True))
    [finding] = ColorVisionRule().evaluate(deck, AnalysisPolicy())
    assert finding.rule_id == "color_vision.contrast" and finding.severity == "review"
    assert "적색약" in finding.message and finding.measured < finding.expected == 3.0
    assert ColorVisionRule().evaluate(deck, AnalysisPolicy(color_vision=False)) == []


def test_fix_candidate_passes_for_every_kind_of_eye():
    deck = _deck(TextRun("경고", 28.0, RED, True))
    findings = ColorVisionRule().evaluate(deck, AnalysisPolicy())
    result = RuleBasedPlanner().plan(deck, findings, AnalysisPolicy())
    colors = [p.after for p in result.proposals if p.prop == "font.color"]
    assert colors and all(worst_vision_contrast(color, "202020") >= 3.0 for color in colors)


def test_colour_only_emphasis_that_vanishes_for_deutan_is_reviewed():
    """흰 배경 위 초록 본문 속 빨간 강조(굵기 같음): 녹색약 눈에는 거의 같은 색."""
    runs = (
        TextRun("기본 노선은 ", 20.0, GREEN, False),
        TextRun("폐지", 20.0, (0xC0, 0x40, 0x20), False),
        TextRun(" 예정입니다", 20.0, GREEN, False),
    )
    deck = _deck(*runs, background=WHITE)
    found = [
        f for f in ColorVisionRule().evaluate(deck, AnalysisPolicy()) if f.rule_id.endswith("color_only")
    ]
    assert found and "폐지" in found[0].evidence
    # 굵게도 했으면 색만으로 강조한 것이 아님
    bold = (runs[0], TextRun("폐지", 20.0, (0xC0, 0x40, 0x20), True), runs[2])
    assert not [
        f
        for f in ColorVisionRule().evaluate(_deck(*bold, background=WHITE), AnalysisPolicy())
        if f.rule_id.endswith("color_only")
    ]


def test_delta_e_is_zero_for_the_same_color():
    assert delta_e(RED, RED) == 0


def test_two_categories_marked_only_by_red_and_green_are_reviewed():
    """함정 PPT 2의 3쪽: '폐지'는 빨강, '유지'는 초록 (굵기 같음) → 녹색약 눈에는 같은 색."""
    dark = (0x22, 0x22, 0x22)
    runs = (
        TextRun("정문: ", 24.0, dark, False),
        TextRun("폐지", 24.0, (0xD6, 0x27, 0x28), False),
        TextRun(" 도서관: ", 24.0, dark, False),
        TextRun("유지", 24.0, (0x2C, 0xA0, 0x2C), False),
    )
    found = [
        f
        for f in ColorVisionRule().evaluate(_deck(*runs, background=WHITE), AnalysisPolicy())
        if f.rule_id.endswith("color_only")
    ]
    assert (
        found and "녹색약" in found[0].message and "폐지" in found[0].evidence and "유지" in found[0].evidence
    )
