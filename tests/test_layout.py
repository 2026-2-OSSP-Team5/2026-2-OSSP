import pytest

from slideguard.checks.layout import LayoutRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
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
):
    runs = (TextRun(text, 20),) if text else ()
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


# --- 안전 여백 ---------------------------------------------------------------
def test_shape_inside_safe_area_has_no_findings():
    assert run_rule(make_shape()) == []


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


def test_missing_geometry_skips_overflow_and_overlap():
    a = make_shape(box=False, shape_id=1)
    b = make_shape(x=100, y=100, shape_id=2)
    findings = run_rule(a, b)
    assert only(findings, "layout.overflow") == []
    assert only(findings, "layout.overlap") == []


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


def test_no_overflow_check_without_rendered_box():
    assert only(run_rule(make_shape()), "layout.overflow") == []


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


def test_text_on_background_panel_is_not_overlap():
    panel = make_shape(x=80, y=80, w=300, h=150, text="", shape_id=1)
    label = make_shape(x=100, y=100, w=200, h=50, shape_id=2)
    assert only(run_rule(panel, label), "layout.overlap") == []


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
    findings = only(run_rule(make_shape(text="가" * 301)), "layout.density")
    assert len(findings) == 1
    assert findings[0].severity == "review"
    assert findings[0].measured == 301
    assert findings[0].expected == 300


def test_character_count_at_limit_is_ok_and_whitespace_ignored():
    text = ("가 " * 150).strip()  # 150자 + 공백
    assert only(run_rule(make_shape(text=text + "나" * 150)), "layout.density") == []


def test_too_many_lines_is_review():
    findings = only(run_rule(make_shape(text="\n".join(["줄"] * 13))), "layout.density")
    assert len(findings) == 1
    assert findings[0].measured == 13
    assert findings[0].expected == 12
    assert findings[0].id.endswith(":lines")


def test_blank_lines_are_not_counted():
    assert only(run_rule(make_shape(text="\n\n".join(["줄"] * 12))), "layout.density") == []


def test_density_sums_all_shapes_on_the_slide():
    a = make_shape(x=100, y=100, text="가" * 200, shape_id=1)
    b = make_shape(x=100, y=300, text="나" * 150, shape_id=2)
    findings = only(run_rule(a, b), "layout.density")
    assert len(findings) == 1
    assert findings[0].measured == 350
    assert findings[0].shape_key == "1:1"  # 가장 글자가 많은 도형


def test_density_is_counted_per_slide():
    a = make_shape(text="가" * 200, slide_number=1)
    b = make_shape(text="나" * 200, slide_number=2)
    assert only(run_rule(a, b), "layout.density") == []


@pytest.mark.parametrize(
    ("purpose", "chars", "expected"),
    [
        (Purpose.PRESENTATION, 350, 1),
        (Purpose.LECTURE, 350, 0),
        (Purpose.REPORT, 700, 0),
        (Purpose.REPORT, 900, 1),
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
