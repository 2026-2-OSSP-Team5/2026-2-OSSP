"""색약 맞춤 검사와 수정 후보."""

from slideguard.checks.color_vision import ColorVisionRule, delta_e, simulate, worst_cvd_contrast
from slideguard.checks.contrast import contrast_ratio
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.planning import RuleBasedPlanner
from slideguard.planning.color import worst_vision_contrast

ALL = AnalysisPolicy(color_vision="all")  # 세 가지 색약 모두 고려 (기본값은 "none")
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
    [finding] = ColorVisionRule().evaluate(deck, ALL)
    assert finding.rule_id == "color_vision.contrast" and finding.severity == "review"
    assert "적색약" in finding.message and finding.measured < finding.expected == 3.0
    assert ColorVisionRule().evaluate(deck, AnalysisPolicy(color_vision="none")) == []


def test_fix_candidate_passes_for_every_kind_of_eye():
    deck = _deck(TextRun("경고", 28.0, RED, True))
    findings = ColorVisionRule().evaluate(deck, ALL)
    result = RuleBasedPlanner().plan(deck, findings, ALL)
    colors = [p.after for p in result.proposals if p.prop == "font.color"]
    # 색약 수정은 큰 글자 기준(3:1)이 아니라 4.5:1까지, 빨강 대신 색각 친화 주황 계열(Okabe & Ito)
    assert colors and all(worst_vision_contrast(color, "202020") >= 4.5 for color in colors)
    assert all("Okabe" in p.note for p in result.proposals)


def test_colour_only_fix_recolours_both_marks_to_cud_colours():
    """'폐지' 빨강 / '유지' 초록 → 모든 눈에서 서로·본문과 구분되는 Okabe & Ito 계열 색으로 함께 바꿈."""
    from slideguard.checks.color_vision import delta_e, simulate
    from slideguard.planning.color import hex_to_rgb

    dark = (0x22, 0x22, 0x22)
    runs = (
        TextRun("정문: ", 24.0, dark, False),
        TextRun("폐지", 24.0, (0xD6, 0x27, 0x28), False),
        TextRun(" 도서관: ", 24.0, dark, False),
        TextRun("유지", 24.0, (0x2C, 0xA0, 0x2C), False),
    )
    deck = _deck(*runs, background=WHITE)
    findings = ColorVisionRule().evaluate(deck, ALL)
    result = RuleBasedPlanner().plan(deck, findings, ALL)
    changes = {p.before: p.after for p in result.proposals}
    assert set(changes) == {"D62728", "2CA02C"} and len({p.group for p in result.proposals}) == 1
    red, green = (hex_to_rgb(changes[key]) for key in ("D62728", "2CA02C"))
    for kind in ("protan", "deutan", "tritan"):
        assert delta_e(simulate(red, kind), simulate(green, kind)) >= 30
    assert all(worst_vision_contrast(color, "FFFFFF") >= 4.5 for color in changes.values())


def test_colour_only_emphasis_that_vanishes_for_deutan_is_reviewed():
    """흰 배경 위 초록 본문 속 빨간 강조(굵기 같음): 녹색약 눈에는 거의 같은 색."""
    runs = (
        TextRun("기본 노선은 ", 20.0, GREEN, False),
        TextRun("폐지", 20.0, (0xC0, 0x40, 0x20), False),
        TextRun(" 예정입니다", 20.0, GREEN, False),
    )
    deck = _deck(*runs, background=WHITE)
    found = [f for f in ColorVisionRule().evaluate(deck, ALL) if f.rule_id.endswith("color_only")]
    assert found and "폐지" in found[0].evidence
    # 굵게도 했으면 색만으로 강조한 것이 아님
    bold = (runs[0], TextRun("폐지", 20.0, (0xC0, 0x40, 0x20), True), runs[2])
    assert not [
        f
        for f in ColorVisionRule().evaluate(_deck(*bold, background=WHITE), ALL)
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
        for f in ColorVisionRule().evaluate(_deck(*runs, background=WHITE), ALL)
        if f.rule_id.endswith("color_only")
    ]
    assert (
        found and "녹색약" in found[0].message and "폐지" in found[0].evidence and "유지" in found[0].evidence
    )


def test_colour_vision_is_off_by_default_and_checks_only_the_chosen_kind():
    """기본값은 '고려 안 함'. 고른 색각 유형의 눈으로만 본다 (빨강이 어두워지는 건 적색약)."""
    deck = _deck(TextRun("경고", 28.0, RED, True))
    assert AnalysisPolicy().color_vision == "none" and AnalysisPolicy().vision_kinds == ()
    assert ColorVisionRule().evaluate(deck, AnalysisPolicy()) == []
    assert ColorVisionRule().evaluate(deck, AnalysisPolicy(color_vision="tritan")) == []
    for choice in ("protan", "red_green", "all"):
        [finding] = ColorVisionRule().evaluate(deck, AnalysisPolicy(color_vision=choice))
        assert "적색약" in finding.message


def test_old_on_off_setting_is_read_as_a_kind():
    assert AnalysisPolicy(color_vision=True).color_vision == "all"
    assert AnalysisPolicy(color_vision=False).color_vision == "none"
    assert AnalysisPolicy(color_vision="red_green").vision_kinds == ("protan", "deutan")


def test_fix_for_red_green_only_needs_to_pass_for_protan_and_deutan():
    deck = _deck(TextRun("경고", 28.0, RED, True))
    policy = AnalysisPolicy(color_vision="red_green")
    findings = ColorVisionRule().evaluate(deck, policy)
    result = RuleBasedPlanner().plan(deck, findings, policy)
    colors = [p.after for p in result.proposals if p.prop == "font.color"]
    assert colors and all(worst_vision_contrast(c, "202020", ("protan", "deutan")) >= 4.5 for c in colors)
