from dataclasses import replace

import pytest

from slideguard.checks.layout import LayoutRule
from slideguard.domain.models import Box, DeckSnapshot, ObjectLocator, Shape, TextRun, TextSpan
from slideguard.domain.policy import AnalysisPolicy, Purpose

# 슬라이드 960x540pt → 안전 여백: 좌우 48pt, 상하 27pt


def make_shape(
    x=100,
    y=100,
    w=200,
    h=50,
    text="본문",
    shape_id=1,
    slide_number=1,
    rendered=None,
    name=None,
    box=True,
    notes=(),
    z_order=None,
    lines=(),
):
    runs = (TextRun(text, 20),) if text else ()
    spans = tuple(TextSpan("line", i, t) for i, t in enumerate(lines, start=1))
    return Shape(
        slide_number=slide_number,
        shape_id=shape_id,
        name=name or f"shape{shape_id}",
        box=Box(x, y, w, h) if box else None,
        text=text,
        runs=runs,
        rendered_box=Box(*rendered) if rendered else None,
        supported=True,
        notes=tuple(notes),
        z_order=z_order,
        spans=spans,
    )


def run_rule(*shapes, policy=None):
    deck = DeckSnapshot(
        source_path="test.pptx",
        source_sha256="0" * 64,
        slide_count=3,
        width_pt=960,
        height_pt=540,
        shapes=tuple(shapes),
    )
    return LayoutRule().evaluate(deck, policy or AnalysisPolicy())


def only(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


@pytest.mark.parametrize("locator", [ObjectLocator((7,), "table_cell", (1, 1)), ObjectLocator((7, 8))])
def test_container_does_not_overlap_its_own_content_but_external_shapes_still_do(locator):
    container = replace(make_shape(text="", shape_id=7), locator=ObjectLocator((7,)))
    child = replace(make_shape(shape_id=8), locator=locator)
    external = replace(make_shape(text="", shape_id=9), locator=ObjectLocator((9,)))

    assert not only(run_rule(container, child), "layout.overlap")
    assert len(only(run_rule(container, child, external), "layout.overlap")) == 1


# --- 안전 여백 ---------------------------------------------------------------
def test_shape_inside_safe_area_has_no_margin_finding():
    assert only(run_rule(make_shape()), "layout.margin") == []


def test_inside_margin_is_review():
    findings = only(run_rule(make_shape(x=20)), "layout.margin")
    assert len(findings) == 1
    assert findings[0].severity == "review"
    assert findings[0].measured == 20.0
    assert findings[0].expected == 48.0
    assert "왼쪽" in findings[0].message


def test_right_and_bottom_margins_are_checked():
    findings = only(run_rule(make_shape(x=800, y=480, w=150, h=50)), "layout.margin")
    assert len(findings) == 1  # 한 도형당 하나로 묶고, 근거에 위반 방향을 모두 적는다
    assert "오른쪽" in findings[0].evidence and "아래" in findings[0].evidence


def test_off_slide_is_warning():
    findings = only(run_rule(make_shape(x=-10)), "layout.margin")
    assert [f.severity for f in findings] == ["warning"]
    assert "슬라이드 밖" in findings[0].message


def test_margin_within_tolerance_is_ignored():
    assert only(run_rule(make_shape(x=47.5)), "layout.margin") == []


def test_margin_ignores_shapes_without_text():
    assert run_rule(make_shape(x=-50, y=-50, w=1100, h=700, text="")) == []


def test_margin_uses_rendered_box_when_available():
    shape = make_shape(x=100, y=100, w=200, h=50, rendered=(10, 100, 200, 50))
    assert [f.severity for f in only(run_rule(shape), "layout.margin")] == ["review"]


# --- 좌표를 확인할 수 없는 객체 (box=None) -----------------------------------
def test_missing_geometry_is_unknown_not_crash():
    shape = make_shape(box=False, notes=("Ancestor transforms prevent verified slide-space geometry",))
    findings = only(run_rule(shape), "layout.margin")
    assert [f.severity for f in findings] == ["unknown"]
    assert findings[0].measured is None
    assert findings[0].box is None
    assert "Ancestor" in findings[0].evidence


def test_missing_geometry_is_unknown_for_overflow_and_overlap():
    """좌표를 몰라 검사하지 못한 것도 판정 불가로 남긴다."""
    findings = run_rule(make_shape(box=False, shape_id=1))
    for rule_id in ("layout.overflow", "layout.overlap"):
        reported = only(findings, rule_id)
        assert [f.severity for f in reported] == ["unknown"], rule_id
        assert reported[0].measured is None
        assert reported[0].evidence


def test_missing_geometry_without_text_is_skipped():
    assert run_rule(make_shape(box=False, text="")) == []


def test_density_still_counts_shapes_without_geometry():
    shape = make_shape(box=False, text="가" * 301)
    assert len(only(run_rule(shape), "layout.density")) == 1


# --- 텍스트 넘침 -------------------------------------------------------------
def test_overflow_is_warning_with_measured_amount():
    shape = make_shape(rendered=(100, 100, 200, 60))  # 아래로 10pt 넘침
    findings = only(run_rule(shape), "layout.overflow")
    assert [f.severity for f in findings] == ["warning"]
    assert findings[0].measured == 10.0
    assert findings[0].expected == 0.0
    assert "아래" in findings[0].message


def test_missing_rendered_box_is_unknown_for_overflow():
    findings = only(run_rule(make_shape()), "layout.overflow")
    assert [f.severity for f in findings] == ["unknown"]
    assert "렌더링된 글자 영역" in findings[0].message


def test_rendered_box_inside_shape_is_not_overflow():
    assert only(run_rule(make_shape(rendered=(110, 105, 150, 30))), "layout.overflow") == []


def test_overflow_within_tolerance_is_ignored():
    assert only(run_rule(make_shape(rendered=(100, 100, 200, 50.5))), "layout.overflow") == []


# --- 겹침 --------------------------------------------------------------------
def test_overlapping_text_boxes_are_review():
    a = make_shape(x=100, y=100, w=200, h=50, shape_id=1)
    b = make_shape(x=250, y=120, w=200, h=50, shape_id=2)
    findings = only(run_rule(a, b), "layout.overlap")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "review"
    assert finding.shape_key == "1:1"
    assert finding.box == Box(250, 120, 50, 30)
    assert finding.measured == 1500.0
    assert "1:2" in finding.evidence


def test_separate_boxes_do_not_overlap():
    a = make_shape(x=100, y=100, shape_id=1)
    b = make_shape(x=100, y=200, shape_id=2)
    assert only(run_rule(a, b), "layout.overlap") == []


def test_touching_edges_do_not_count_as_overlap():
    a = make_shape(x=100, y=100, w=200, h=50, shape_id=1)
    b = make_shape(x=300, y=100, w=200, h=50, shape_id=2)
    assert only(run_rule(a, b), "layout.overlap") == []


def test_panel_behind_text_is_not_overlap():
    """뒤에 있는 배경 패널만 제외한다."""
    panel = make_shape(x=80, y=80, w=300, h=150, text="", shape_id=1, z_order=1)
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2, z_order=2)
    assert only(run_rule(panel, label), "layout.overlap") == []


def test_shape_in_front_of_text_is_reported():
    """앞에서 텍스트를 가리는 도형은 검토 대상이다."""
    cover = make_shape(x=80, y=80, w=300, h=150, text="", shape_id=1, z_order=5, name="가림막")
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2, z_order=2)
    findings = only(run_rule(cover, label), "layout.overlap")
    assert len(findings) == 1
    assert findings[0].severity == "review"
    assert "가립니다" in findings[0].message
    assert findings[0].shape_key == "1:2"


def test_unknown_z_order_is_unknown_not_review():
    """앞뒤를 모르면 배경인지 가림인지 판정할 수 없으므로 판정 불가로 남긴다."""
    panel = make_shape(x=80, y=80, w=300, h=150, text="", shape_id=1)
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2)
    findings = only(run_rule(panel, label), "layout.overlap")
    assert [f.severity for f in findings] == ["unknown"]
    assert findings[0].measured is None
    assert "z_order" in findings[0].evidence


def test_partial_overlap_without_z_order_is_still_review():
    """감싸지 않은 평범한 겹침은 앞뒤를 몰라도 검토 대상이다."""
    a = make_shape(x=100, y=100, w=200, h=50, shape_id=1)
    b = make_shape(x=250, y=120, w=200, h=50, shape_id=2)
    assert [f.severity for f in only(run_rule(a, b), "layout.overlap")] == ["review"]


def test_text_partially_over_non_text_shape_is_review_anchored_on_text():
    picture = make_shape(x=250, y=100, w=200, h=100, text="", shape_id=1, name="그림")
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2)
    findings = only(run_rule(picture, label), "layout.overlap")
    assert [f.shape_key for f in findings] == ["1:2"]


def test_two_non_text_shapes_are_ignored():
    a = make_shape(x=100, y=100, text="", shape_id=1)
    b = make_shape(x=150, y=110, text="", shape_id=2)
    assert only(run_rule(a, b), "layout.overlap") == []


def test_text_shape_containing_other_text_is_review():
    outer = make_shape(x=80, y=80, w=300, h=150, shape_id=1)
    inner = make_shape(x=100, y=100, w=100, h=30, shape_id=2)
    assert len(only(run_rule(outer, inner), "layout.overlap")) == 1


def test_shapes_on_different_slides_are_not_compared():
    a = make_shape(slide_number=1, shape_id=1)
    b = make_shape(slide_number=2, shape_id=1)
    assert only(run_rule(a, b), "layout.overlap") == []


# --- 정보 밀도 ---------------------------------------------------------------
def test_too_many_characters_is_review():
    findings = only(run_rule(make_shape(text="가" * 101)), "layout.density")
    assert len(findings) == 1
    assert findings[0].severity == "review"
    assert findings[0].measured == 101
    assert findings[0].expected == 100


def test_character_count_at_limit_is_ok_and_whitespace_ignored():
    text = ("가 " * 50).strip()  # 50자 + 공백
    assert only(run_rule(make_shape(text=text + "나" * 50)), "layout.density") == []


def test_too_many_lines_is_review():
    findings = only(run_rule(make_shape(text="\n".join(["줄"] * 7))), "layout.density")
    assert len(findings) == 1
    assert findings[0].measured == 7
    assert findings[0].expected == 6
    assert findings[0].id.endswith(":lines")


def test_six_lines_is_allowed():
    """6x6 법칙의 상한 자체는 통과한다."""
    assert only(run_rule(make_shape(text="\n".join(["줄"] * 6))), "layout.density") == []


def test_blank_lines_are_not_counted():
    assert only(run_rule(make_shape(text="\n\n".join(["줄"] * 6))), "layout.density") == []


def test_density_sums_all_shapes_on_the_slide():
    a = make_shape(x=100, y=100, text="가" * 200, shape_id=1)
    b = make_shape(x=100, y=300, text="나" * 150, shape_id=2)
    findings = only(run_rule(a, b), "layout.density")
    assert len(findings) == 1
    assert findings[0].measured == 350
    assert findings[0].shape_key == "1:1"  # 가장 글자가 많은 도형


def test_density_is_counted_per_slide():
    a = make_shape(text="가" * 60, slide_number=1)
    b = make_shape(text="나" * 60, slide_number=2)
    assert only(run_rule(a, b), "layout.density") == []


@pytest.mark.parametrize(
    ("purpose", "chars", "expected"),
    [
        (Purpose.PRESENTATION, 110, 1),
        (Purpose.LECTURE, 110, 0),
        (Purpose.LECTURE, 160, 1),
        (Purpose.REPORT, 250, 0),
        (Purpose.REPORT, 310, 1),
    ],
)
def test_density_threshold_depends_on_purpose(purpose, chars, expected):
    findings = run_rule(make_shape(text="가" * chars), policy=AnalysisPolicy(purpose=purpose))
    assert len(only(findings, "layout.density")) == expected


# --- 공통 --------------------------------------------------------------------
def test_slide_filter_limits_scope():
    bad = dict(x=-10)
    findings = run_rule(
        make_shape(slide_number=1, **bad),
        make_shape(slide_number=2, **bad),
        policy=AnalysisPolicy(slides=(2,)),
    )
    assert {f.shape_key for f in findings} == {"2:1"}


def test_finding_ids_are_unique():
    shapes = [
        make_shape(x=20, shape_id=1, text="가" * 200),
        make_shape(x=60, y=110, shape_id=2, text="나" * 200),
    ]
    findings = run_rule(*shapes)
    assert len({f.id for f in findings}) == len(findings)
    assert all(f.rule_id.startswith("layout.") for f in findings)


# --- 확인 요청 반영분 --------------------------------------------------------
def test_off_slide_message_points_at_the_side_that_left_the_slide():
    """여백만 침범한 변이 아니라 실제로 벗어난 변을 보고한다.

    왼쪽은 여백(48pt)을 크게 침범했지만 슬라이드 안에 있고, 아래만 3pt 벗어났다.
    여백 부족분으로 고르면 "왼쪽 2.0pt"가 나와, 밖으로 나갔다면서 양수를 적게 된다.
    """
    shape = make_shape(x=2, y=493, w=200, h=50)
    findings = only(run_rule(shape), "layout.margin")
    assert [f.severity for f in findings] == ["warning"]
    assert "아래" in findings[0].message
    assert findings[0].measured == -3.0
    assert findings[0].measured < 0  # 밖으로 나갔으니 음수여야 한다


def test_off_slide_evidence_still_lists_every_violated_side():
    """보고하는 방향은 하나지만, 근거에는 위반한 변을 모두 남긴다."""
    findings = only(run_rule(make_shape(x=2, y=493, w=200, h=50)), "layout.margin")
    assert "왼쪽" in findings[0].evidence and "아래" in findings[0].evidence


def test_rendered_lines_catch_wrapped_text():
    """원문은 2줄이지만 자동 줄바꿈으로 7줄이 된 경우를 잡는다."""
    shape = make_shape(text="긴 문단\n또 다른 문단", lines=[f"줄{i}" for i in range(1, 8)])
    findings = only(run_rule(shape), "layout.density")
    assert len(findings) == 1
    assert findings[0].measured == 7
    assert findings[0].expected == 6
    assert "자동 줄바꿈 반영" in findings[0].evidence


def test_rendered_lines_within_limit_pass():
    shape = make_shape(text="한 줄", lines=[f"줄{i}" for i in range(1, 7)])
    assert only(run_rule(shape), "layout.density") == []


def test_falls_back_to_source_newlines_without_spans():
    findings = only(run_rule(make_shape(text="\n".join(["줄"] * 7))), "layout.density")
    assert findings[0].measured == 7
    assert "원문 개행 기준" in findings[0].evidence


def test_line_count_uses_the_larger_of_source_and_rendered():
    """reader가 줄을 일부만 주는 경우에도 원문 개행만큼은 센다."""
    shape = make_shape(text="\n".join(["줄"] * 7), lines=["a", "b"])
    findings = only(run_rule(shape), "layout.density")
    assert findings[0].measured == 7


def _with_lines(shape, *boxes):
    """PowerPoint에서 읽은 줄 위치(BoundLeft/Top/Width/Height)를 붙인다."""
    spans = tuple(TextSpan("line", i, f"줄{i}", Box(*box)) for i, box in enumerate(boxes, start=1))
    return replace(shape, spans=spans)


def test_text_frames_overlap_but_lines_do_not_is_not_overlap():
    """상자 테두리만 겹치고 글자 줄은 안 겹치면 겹침이 아니다 (짧은 마지막 줄 옆에 다른 상자)."""
    upper = _with_lines(
        make_shape(x=100, y=100, w=400, h=60, shape_id=1, rendered=(100, 100, 400, 60)),
        (100, 100, 400, 30),  # 긴 첫 줄
        (100, 130, 120, 30),  # 짧은 마지막 줄
    )
    lower = _with_lines(
        make_shape(x=300, y=140, w=200, h=40, shape_id=2, rendered=(300, 140, 200, 40)),
        (300, 140, 200, 40),
    )
    assert only(run_rule(upper, lower), "layout.overlap") == []


def test_lines_that_really_collide_are_still_review():
    a = _with_lines(
        make_shape(x=100, y=100, w=300, h=40, shape_id=1, rendered=(100, 100, 300, 40)), (100, 100, 300, 40)
    )
    b = _with_lines(
        make_shape(x=250, y=120, w=300, h=40, shape_id=2, rendered=(250, 120, 300, 40)), (250, 120, 300, 40)
    )
    findings = only(run_rule(a, b), "layout.overlap")
    assert [f.severity for f in findings] == ["review"]
    assert findings[0].measured == 150 * 20  # 실제로 겹친 줄 넓이


def test_bubble_partly_behind_text_is_background_not_overlap():
    """글자 뒤에 일부만 깔린 말풍선·카드·사진은 배경 (글씨가 보이는지는 명암비 검사가 본다)."""
    bubble = make_shape(x=250, y=90, w=300, h=100, text="", shape_id=1, z_order=1, name="말풍선")
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2, z_order=2)
    assert only(run_rule(bubble, label), "layout.overlap") == []
    cover = replace(bubble, z_order=5)  # 앞에서 가리면 여전히 검토
    assert "가립니다" in only(run_rule(cover, label), "layout.overlap")[0].message


def test_hanging_punctuation_and_descent_are_not_overflow():
    """40pt 글자의 줄 끝 쉼표가 9.7pt 밖으로 나가거나 아래로 1.8pt 걸친 정도는 넘침이 아니다 (자바게티 7쪽)."""
    shape = replace(
        make_shape(x=255.6, y=99.0, w=932.4, h=104.2, rendered=(262.8, 102.6, 935.3, 103.0)),
        runs=(TextRun("보유 도서의 상태를 쉽게 등록,", 40),),
    )
    assert only(run_rule(shape), "layout.overflow") == []
    wide = replace(shape, rendered_box=Box(262.8, 102.6, 960.0, 96.0))  # 오른쪽 35pt: 진짜 넘침
    assert [f.message for f in only(run_rule(wide), "layout.overflow")] == [
        "텍스트가 도형 영역을 벗어납니다 (오른쪽)"
    ]


def test_density_far_over_the_limit_is_a_problem_not_just_review():
    """기준(발표용 6줄)의 2배 이상이면 빨간 '문제'로 (자바게티 22쪽 참고문헌처럼 20줄 넘는 글 벽)."""
    lines = [f"{i}. https://blog.example.com/{i}" for i in range(1, 21)]
    wall = make_shape(text="\n".join(lines), shape_id=1)
    some = make_shape(text="\n".join(f"항목 {i}" for i in range(8)), shape_id=1)  # 8줄·짧은 글
    assert {f.severity for f in only(run_rule(wall), "layout.density")} == {"warning"}
    assert {f.severity for f in only(run_rule(some), "layout.density")} == {"review"}


def test_card_holding_text_that_runs_off_the_slide_is_a_problem():
    """함정 PPT 2쪽: 글자는 슬라이드 안이지만 그 글을 감싼 카드가 오른쪽 끝을 넘으면 문제."""
    card = make_shape(x=655, y=151, w=331, h=281, text="", shape_id=3)
    body = make_shape(x=680, y=238, w=270, h=173, shape_id=4)
    found = [f for f in only(run_rule(card, body), "layout.margin") if f.id.endswith(":card")]
    assert [(f.shape_key, f.severity, f.measured) for f in found] == [("1:3", "warning", -26.0)]


def test_full_bleed_background_or_empty_decoration_off_slide_is_fine():
    banner = make_shape(x=-10, y=0, w=980, h=120, text="", shape_id=3)  # 가장자리까지 꽉 채운 띠
    title = make_shape(x=60, y=30, w=500, h=60, shape_id=4)
    blob = make_shape(x=900, y=400, w=200, h=200, text="", shape_id=5)  # 글이 없는 장식
    assert [f for f in run_rule(banner, title, blob) if f.id.endswith(":card")] == []


def test_table_cell_text_never_counts_as_overflow():
    """표 칸은 행이 저절로 늘어나므로 넘치지 않는다 (칸 글자 픽셀 측정이 틀려도 '655pt 넘침'이 안 나오게)."""
    cell = replace(
        make_shape(x=60, y=200, w=300, h=40, rendered=(60, 80, 1240, 450)),
        locator=ObjectLocator((7,), "table_cell", (2, 1)),
    )
    assert only(run_rule(cell), "layout.overflow") == []


def test_density_ignores_blank_lines_chart_labels_and_footers():
    """빈 줄·폭 없는 공백 줄, 그래프 축 눈금, 쪽 번호는 '읽을 내용'의 줄로 세지 않는다 (함정 PPT 3쪽)."""
    body = make_shape(text="첫째 줄\n\n \n​\n둘째 줄", shape_id=1)
    axis = replace(
        make_shape(text="\n".join(f"{hour}시" for hour in range(7, 19)), shape_id=2),
        locator=ObjectLocator((2,), "axis_title", (1, 1)),
    )
    page = replace(make_shape(text="3", shape_id=3), placeholder_type=13)
    number = make_shape(text="02", shape_id=4)  # 쪽 왼쪽 위 번호 표시
    lines = make_shape(text="\n".join(["항목"] * 3), shape_id=5)
    assert only(run_rule(body, axis, page, number, lines), "layout.density") == []  # 2 + 3 = 5줄


# --- 한 줄 길이 (WCAG 1.4.8: 80자, CJK 40자) -----------------------------------


def line_findings(findings):
    return [f for f in findings if f.rule_id == "layout.line_length"]


def test_short_korean_line_passes():
    assert line_findings(run_rule(make_shape(lines=("가" * 40,)))) == []


def test_long_korean_line_is_review():
    findings = line_findings(run_rule(make_shape(lines=("가" * 41,))))
    assert [f.severity for f in findings] == ["review"]
    finding = findings[0]
    assert finding.measured == 41
    assert finding.expected == 40.0
    assert "1.4.8" in finding.evidence


def test_latin_counts_half_so_eighty_is_the_limit():
    # WCAG의 두 숫자(80자 / CJK 40자)는 같은 폭을 가리킨다.
    assert line_findings(run_rule(make_shape(lines=("a" * 80,)))) == []
    assert len(line_findings(run_rule(make_shape(lines=("a" * 82,))))) == 1


def test_mixed_script_line_is_measured_by_width():
    # 한글 30자(30) + 영문 20자(10) = 40 → 경계값이라 통과
    assert line_findings(run_rule(make_shape(lines=("가" * 30 + "a" * 20,)))) == []
    assert len(line_findings(run_rule(make_shape(lines=("가" * 30 + "a" * 22,))))) == 1


def test_only_the_widest_line_is_reported():
    shape = make_shape(lines=("가" * 50, "가" * 45, "짧은 줄"))
    findings = line_findings(run_rule(shape))
    assert len(findings) == 1  # 줄마다 보고하지 않는다
    assert findings[0].measured == 50


def test_source_newlines_are_not_counted():
    # 렌더링된 줄이 없으면 어디서 접히는지 알 수 없으므로 판정하지 않는다.
    assert line_findings(run_rule(make_shape(text="가" * 100))) == []


def test_trailing_spaces_do_not_make_a_line_long():
    assert line_findings(run_rule(make_shape(lines=("가" * 38 + "    ",)))) == []


def test_footer_and_chart_text_are_skipped():
    # 쪽 번호·차트 글자는 읽을 내용이 아니라 정보 밀도에서 빼는 것과 같은 기준을 쓴다.
    footer = replace(make_shape(lines=("가" * 60,)), placeholder_type=13)
    assert line_findings(run_rule(footer)) == []
