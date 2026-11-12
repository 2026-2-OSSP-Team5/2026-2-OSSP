"""수정 후보 생성 동작 테스트."""

from slideguard.planning import Category, Property, RuleBasedPlanner
from slideguard.planning.color import (
    blend,
    contrast_ratio,
    harmony_score,
    nearest_passing_color,
    worst_contrast_over_unknown,
)
from tests.fakes import FakeFinding, FakePolicy, FakeRun, FakeShape, FakeSnapshot


def deck(*shapes):
    return FakeSnapshot(shapes=list(shapes))


def text_shape(color="999999", size=24.0, bold=False, **geometry):
    return FakeShape(1, 5, [FakeRun("글자", size, color, bold)], **geometry)


def contrast(fg="999999", bg="FFFFFF", status="warning", kind=None):
    measured = {"foreground": fg}
    if bg is not None:
        measured["background"] = bg
        measured["ratio"] = contrast_ratio(fg, bg)
    if kind is not None:
        measured["background_kind"] = kind
    return FakeFinding("contrast", 1, 5, status, measured)


def plan(snapshot, findings, **policy):
    return RuleBasedPlanner().plan(snapshot, findings, FakePolicy(**policy))


# ---------------------------------------------------------------- 색 계산


def test_known_contrast_values():
    assert round(contrast_ratio("000000", "FFFFFF"), 2) == 21.0
    assert round(contrast_ratio("777777", "FFFFFF"), 2) == 4.48


def test_nearest_color_passes_and_stays_close():
    new = nearest_passing_color("999999", "FFFFFF", 4.5)
    assert contrast_ratio(new, "FFFFFF") >= 4.5
    assert new != "000000"


def test_scrim_worst_case_covers_any_background():
    worst = worst_contrast_over_unknown("FFFFFF", "000000", 0.6)
    for under in ("FFFFFF", "000000", "808080", "FFCC00"):
        assert contrast_ratio("FFFFFF", blend("000000", under, 0.6)) >= worst - 0.01


def test_harmony_score_separates_clashing_pair():
    assert harmony_score("333333", "FFFFFF") > 0  # 진회색/흰색: 조화
    assert harmony_score("FF0000", "00FF00") < 0  # 빨강/초록: 부조화


# ---------------------------------------------------------------- 수정 수준 (회의록: 최소/중간/최대)


def test_gray_body_text_is_fixed_even_at_passive():
    result = plan(deck(text_shape("999999")), [contrast("999999")], strength="passive", purpose="report")
    p = result.proposals[0]
    assert p.category == Category.READABILITY
    assert contrast_ratio(p.after, "FFFFFF") >= 4.5


def test_brand_color_text_left_at_passive_fixed_from_balanced():
    finding = contrast("2E86DE")
    passive = plan(deck(text_shape("2E86DE")), [finding], strength="passive")
    assert passive.proposals == []
    assert "디자인 영역으로 보고 유지" in passive.skipped[0].reason

    balanced = plan(deck(text_shape("2E86DE")), [finding], strength="balanced")
    assert balanced.proposals[0].category == Category.DESIGN
    assert contrast_ratio(balanced.proposals[0].after, "FFFFFF") >= 4.5  # B 기준 (일반 글자)


def test_position_changes_need_balanced_level():
    picture = FakeShape(1, 9, [], left=0, top=0, width=500, height=300, kind="picture")
    finding = FakeFinding("hidden_text", 1, 5, "warning", {"cover_shape_id": 9})

    passive = plan(deck(text_shape(), picture), [finding], strength="passive")
    assert passive.proposals == []
    assert "항목 위치" in passive.skipped[0].reason and "중간 수정 이상" in passive.skipped[0].reason

    p = plan(deck(text_shape(), picture), [finding], strength="balanced").proposals[0]
    assert p.prop == Property.Z_ORDER
    assert "명암비 확인" in p.note


def test_format_changes_need_aggressive_level():
    finding = FakeFinding("line_spacing", 1, 5, "warning", {"line_spacing": 0.8, "required_spacing": 1.0})
    balanced = plan(deck(text_shape()), [finding], strength="balanced")
    assert "서식" in balanced.skipped[0].reason
    p = plan(deck(text_shape()), [finding], strength="aggressive").proposals[0]
    assert (p.prop, p.after) == (Property.LINE_SPACING, 1.0)


def test_photo_text_gets_scrim_only_at_aggressive():
    shape = text_shape("FFFFFF", size=32, bold=True)
    finding = contrast("FFFFFF", bg=None, status="unmeasurable", kind="image")
    assert plan(deck(shape), [finding], strength="passive").proposals == []
    assert "배경색" in plan(deck(shape), [finding], strength="balanced").skipped[0].reason

    p = plan(deck(shape), [finding], strength="aggressive").proposals[0]
    assert p.prop == Property.SHAPE_FILL
    alpha = 1 - p.after["transparency"]
    assert worst_contrast_over_unknown("FFFFFF", p.after["color"], alpha) >= 3.0  # B 기준 (큰 글자)


def test_small_design_budget_does_not_lower_the_standard():
    # 연한 하늘색 브랜드 글씨를 6:1까지 진하게 하려면 큰 변경 → 중간 수정에서는 타협하지 않고 남김
    result = plan(deck(text_shape("4FC3F7")), [contrast("4FC3F7")], strength="balanced")
    assert result.proposals == []
    assert "디자인 영역 수정 예산 초과" in result.skipped[0].reason


def test_unmeasurable_without_photo_info_is_not_guessed():
    finding = contrast("FFFFFF", bg=None, status="unmeasurable")
    result = plan(deck(text_shape("FFFFFF")), [finding], strength="aggressive")
    assert "측정 불가" in result.skipped[0].reason


# ---------------------------------------------------------------- 사용자 지정 (회의록 체크박스)


def test_user_lock_font_size_blocks_size_and_autofit():
    size = FakeFinding("font_size", 1, 5, "warning", {"size_pt": 10.0})
    autofit = FakeFinding(
        "autofit_shrink", 1, 5, "warning", {"specified_size_pt": 24.0, "effective_size_pt": 12.0}
    )
    result = plan(deck(text_shape()), [size, autofit], strength="aggressive", user_locks=("font_size",))
    assert result.proposals == []
    assert all("고정" in s.reason for s in result.skipped)


def test_user_lock_layout_blocks_moves():
    shape = text_shape(left=800, top=100, width=300, height=60)
    result = plan(
        deck(shape), [FakeFinding("off_slide", 1, 5)], strength="aggressive", user_locks=("layout",)
    )
    assert result.proposals == []


def test_excluded_rule_is_skipped():
    result = plan(deck(text_shape()), [contrast()], strength="aggressive", excluded_rules=("contrast",))
    assert "제외" in result.skipped[0].reason


# ---------------------------------------------------------------- 기준값 (모두 B에서)


def test_font_size_target_comes_from_b_policy_by_purpose():
    from slideguard.domain.policy import Purpose, font_size_threshold

    finding = FakeFinding("font_size", 1, 5, "warning", {"size_pt": 9.0})
    for purpose in Purpose:
        result = plan(deck(text_shape()), [finding], purpose=purpose.value)
        assert result.proposals[0].after == font_size_threshold(purpose).minimum_pt


def test_finding_expected_value_takes_priority_and_result_carries_b_standards():
    finding = FakeFinding("font_size", 1, 5, "warning", {"size_pt": 9.0, "required_pt": 16.0})
    result = plan(deck(text_shape()), [finding])
    assert result.proposals[0].after == 16.0
    assert result.standards.min_font_pt.basis.startswith("B 기준")


def test_rules_without_b_criterion_get_no_proposal():
    spacing = FakeFinding("line_spacing", 1, 5, "warning", {"line_spacing": 0.8})
    result = plan(deck(text_shape()), [spacing], strength="aggressive")
    assert result.proposals == []
    assert len(result.skipped) == 1
    assert "B에" in result.skipped[0].reason


def test_text_already_meeting_standard_is_never_shrunk():
    finding = FakeFinding("font_size", 1, 5, "warning", {"size_pt": 18.0})
    result = plan(deck(text_shape()), [finding], purpose="report")
    assert result.proposals == []
    assert "줄이는 후보는 만들지 않음" in result.skipped[0].reason


# ---------------------------------------------------------------- 색 후보 (회의록: 명도 조정 / 표준 팔레트)


def test_palette_color_offered_as_alternative():
    result = plan(deck(text_shape("999999")), [contrast("999999")], strength="aggressive")
    colors = [p.after for p in result.proposals + result.alternatives if p.prop == Property.FONT_COLOR]
    assert "333333" in colors
    assert all(p.harmony is not None for p in result.proposals if p.prop == Property.FONT_COLOR)


# ---------------------------------------------------------------- 객체 충돌


def test_text_overlap_is_review_only_below_aggressive():
    other = FakeShape(1, 6, [FakeRun("x", 24, "000000", False)], left=80, top=230, width=800, height=80)
    finding = FakeFinding("text_overlap", 1, 5, "warning", {"other_shape_id": 6})
    for strength in ("passive", "balanced"):
        result = plan(
            deck(text_shape(left=80, top=200, width=800, height=80), other), [finding], strength=strength
        )
        assert result.proposals == []
        assert result.skipped[0].category == Category.REVIEW_ONLY


def test_text_overlap_separated_at_aggressive():
    other = FakeShape(1, 6, [FakeRun("x", 24, "000000", False)], left=80, top=230, width=800, height=80)
    finding = FakeFinding("text_overlap", 1, 5, "warning", {"other_shape_id": 6})
    shape = text_shape(left=80, top=200, width=800, height=80)
    p = plan(deck(shape, other), [finding], strength="aggressive").proposals[0]
    assert p.prop == Property.SHAPE_POSITION
    assert p.after == (80, 150)  # 위로 50pt (아래로 110pt보다 가까움)


def test_text_overlap_without_free_space_is_left():
    # 슬라이드 전체를 덮는 글상자와 겹치면 옮길 빈자리가 없음
    other = FakeShape(1, 6, [FakeRun("x", 24, "000000", False)], left=0, top=0, width=960, height=540)
    finding = FakeFinding("text_overlap", 1, 5, "warning", {"other_shape_id": 6})
    result = plan(deck(text_shape(), other), [finding], strength="aggressive")
    assert "빈자리가 없음" in result.skipped[0].reason


def test_overflow_grows_box_when_space_is_free():
    finding = FakeFinding("overflow", 1, 5, "warning", {"needed_height": 90})
    p = plan(deck(text_shape(top=100, height=60)), [finding]).proposals[0]
    assert (p.prop, p.before, p.after) == (Property.SHAPE_HEIGHT, 60, 90)


def test_overflow_blocked_by_picture_is_left_below_aggressive():
    below = FakeShape(1, 7, [], left=50, top=170, width=400, height=50, kind="picture")
    finding = FakeFinding("overflow", 1, 5, "warning", {"needed_height": 90})
    result = plan(deck(text_shape(top=100, height=60), below), [finding], strength="balanced")
    assert "ID 7" in result.skipped[0].reason


def test_overflow_pushes_picture_down_at_aggressive():
    below = FakeShape(1, 7, [], left=50, top=170, width=400, height=50, kind="picture")
    finding = FakeFinding("overflow", 1, 5, "warning", {"needed_height": 90})
    result = plan(deck(text_shape(top=100, height=60), below), [finding], strength="aggressive")
    height, push = result.proposals
    assert (height.shape_id, height.prop, height.after) == (5, Property.SHAPE_HEIGHT, 90)
    assert (push.shape_id, push.prop, push.after) == (7, Property.SHAPE_POSITION, (50, 190))  # 20pt 내림
    assert height.group == push.group is not None  # 함께 적용하는 묶음


def test_overflow_push_refused_when_picture_would_leave_slide():
    below = FakeShape(1, 7, [], left=50, top=170, width=400, height=360, kind="picture")  # 아래 끝 530
    finding = FakeFinding("overflow", 1, 5, "warning", {"needed_height": 90})
    result = plan(deck(text_shape(top=100, height=60), below), [finding], strength="aggressive")
    assert result.proposals == []
    assert "슬라이드 아래로" in result.skipped[0].reason


def test_full_slide_background_picture_does_not_block():
    background = FakeShape(1, 2, [], left=0, top=0, width=960, height=540, kind="picture")
    finding = FakeFinding("overflow", 1, 5, "warning", {"needed_height": 90})
    assert plan(deck(text_shape(top=100, height=60), background), [finding]).proposals


def test_off_slide_box_moves_into_safe_area():
    shape = text_shape(left=800, top=100, width=300, height=60)
    p = plan(deck(shape), [FakeFinding("off_slide", 1, 5)]).proposals[0]
    assert p.prop == Property.SHAPE_POSITION
    assert p.after == (612, 100)  # 960 - 5% 여백(48) - 폭 300


def test_off_slide_move_blocked_by_collision():
    shape = text_shape(left=800, top=100, width=300, height=60)
    neighbor = FakeShape(1, 8, [FakeRun("x", 24, "000000", False)], left=600, top=100, width=150, height=60)
    result = plan(deck(shape, neighbor), [FakeFinding("off_slide", 1, 5)], strength="aggressive")
    assert "ID 8" in result.skipped[0].reason


# ---------------------------------------------------------------- 회의록 검사 항목: 여백·글꼴·밀도·위계


def test_text_in_edge_margin_moves_inward():
    shape = text_shape(left=10, top=500, width=300, height=30)
    p = plan(deck(shape), [FakeFinding("safe_margin", 1, 5)]).proposals[0]
    assert p.after == (48, 483)  # 가장자리 5% (가로 48pt, 세로 27pt) 안쪽


def test_decorative_font_unified_only_at_aggressive():
    finding = FakeFinding("font_family", 1, 5, "warning", {"font_name": "궁서체"})
    assert plan(deck(text_shape()), [finding], strength="passive").proposals == []
    assert plan(deck(text_shape()), [finding], strength="balanced").proposals == []  # 비용 0.3 > 0.15
    p = plan(deck(text_shape()), [finding], strength="aggressive").proposals[0]
    assert (p.prop, p.after) == (Property.FONT_FAMILY, "맑은 고딕")


def test_density_is_reported_not_auto_fixed():
    finding = FakeFinding("density", 1, 5, "warning", {"line_count": 9, "max_words": 11})
    result = plan(deck(text_shape()), [finding], strength="aggressive")
    assert result.proposals == []
    assert "9줄" in result.skipped[0].reason and "6줄" in result.skipped[0].reason


def test_hierarchy_title_enlarged_only_at_aggressive():
    finding = FakeFinding(
        "hierarchy", 1, 5, "warning", {"title_size_pt": 24.0, "body_size_pt": 24.0, "required_ratio": 1.25}
    )
    assert plan(deck(text_shape()), [finding], strength="balanced").proposals == []
    p = plan(deck(text_shape()), [finding], strength="aggressive").proposals[0]
    assert (p.prop, p.after) == (Property.FONT_SIZE, 30.0)


# ---------------------------------------------------------------- 그 밖의 가독성 항목


def test_autofit_shrink_below_minimum_is_turned_off():
    finding = FakeFinding(
        "autofit_shrink", 1, 5, "warning", {"specified_size_pt": 24.0, "effective_size_pt": 12.0}
    )
    p = plan(deck(text_shape()), [finding], strength="aggressive").proposals[0]
    assert (p.prop, p.before, p.after) == (Property.AUTOFIT, "shrink", "none")


def test_locked_shape_gets_no_proposal():
    result = plan(deck(text_shape()), [contrast()], strength="aggressive", locked_shapes=frozenset({(1, 5)}))
    assert "고정" in result.skipped[0].reason


# ---------------------------------------------------------------- 색은 모든 수준에서 원래 색 쪽으로


def test_aggressive_still_keeps_color_close_to_original():
    for strength in ("balanced", "aggressive"):
        p = plan(deck(text_shape("999999")), [contrast("999999")], strength=strength).proposals[0]
        assert p.after not in ("000000", "333333")
        assert contrast_ratio(p.after, "FFFFFF") >= 4.5  # B 기준 (일반 글자)
