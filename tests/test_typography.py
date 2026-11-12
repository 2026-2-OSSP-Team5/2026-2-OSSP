import pytest

from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy, Purpose


def make_shape(
    runs,
    supported=True,
    slide_number=1,
    shape_id=1,
    text=None,
    notes=(),
    placeholder_type=None,
):
    run_tuple = tuple(runs)
    return Shape(
        slide_number=slide_number,
        shape_id=shape_id,
        name=f"shape{shape_id}",
        box=Box(0, 0, 100, 50),
        text="".join(r.text for r in run_tuple) if text is None else text,
        runs=run_tuple,
        supported=supported,
        notes=tuple(notes),
        placeholder_type=placeholder_type,
    )


def title(size_pt, *, slide_number=1, shape_id=1, kind=1):
    runs = [TextRun("제목", size_pt)] if size_pt is not None else [TextRun("제목", None)]
    return make_shape(runs, slide_number=slide_number, shape_id=shape_id, placeholder_type=kind)


def body(size_pt, *, slide_number=1, shape_id=2, kind=2):
    runs = [TextRun("본문", size_pt)] if size_pt is not None else [TextRun("본문", None)]
    return make_shape(runs, slide_number=slide_number, shape_id=shape_id, placeholder_type=kind)


def hierarchy_findings(findings):
    return [f for f in findings if f.id.endswith(":hierarchy")]


def run_rule(*shapes, policy=None):
    deck = DeckSnapshot(
        source_path="test.pptx",
        source_sha256="0" * 64,
        slide_count=3,
        width_pt=960,
        height_pt=540,
        shapes=tuple(shapes),
    )
    return TypographyRule().evaluate(deck, policy or AnalysisPolicy())


def severities(findings):
    return [f.severity for f in findings]


def test_recommended_size_has_no_findings():
    assert run_rule(make_shape([TextRun("본문", 18)])) == []
    assert run_rule(make_shape([TextRun("본문", 24)])) == []


def test_below_minimum_is_warning_for_presentation():
    findings = run_rule(make_shape([TextRun("각주", 12)]))
    assert severities(findings) == ["warning"]
    finding = findings[0]
    assert finding.rule_id == "typography"
    assert finding.shape_key == "1:1"
    assert finding.measured == 12
    assert finding.expected == 14.0
    assert "발표용" in finding.evidence


def test_between_minimum_and_recommended_is_review():
    findings = run_rule(make_shape([TextRun("본문", 16)]))
    assert severities(findings) == ["review"]
    assert findings[0].expected == 18.0


def test_exactly_at_minimum_is_review_not_warning():
    assert severities(run_rule(make_shape([TextRun("본문", 14)]))) == ["review"]


def test_unknown_size_is_unknown_not_pass():
    findings = run_rule(make_shape([TextRun("본문", None)]))
    assert severities(findings) == ["unknown"]
    assert findings[0].measured is None


def test_unsupported_shape_is_unknown_with_notes():
    findings = run_rule(make_shape([TextRun("본문", 20)], supported=False, notes=("표 안의 텍스트",)))
    assert severities(findings) == ["unknown"]
    assert "표 안의 텍스트" in findings[0].evidence


def test_shape_without_text_is_skipped():
    assert run_rule(make_shape([], text="", supported=False)) == []
    assert run_rule(make_shape([TextRun("  ", 8)], text=" ")) == []


def test_text_without_run_info_is_unknown():
    assert severities(run_rule(make_shape([], text="본문"))) == ["unknown"]


def test_same_size_runs_reported_once():
    runs = [TextRun("가", 12), TextRun("나", 12)]
    assert len(run_rule(make_shape(runs))) == 1


def test_different_sizes_reported_separately_with_unique_ids():
    findings = run_rule(make_shape([TextRun("가", 10), TextRun("나", 16)]))
    assert severities(findings) == ["warning", "review"]
    assert len({f.id for f in findings}) == 2


def test_unknown_size_reported_once_per_shape():
    findings = run_rule(make_shape([TextRun("가", None), TextRun("나", None)]))
    assert severities(findings) == ["unknown"]


def test_unknown_and_known_sizes_can_coexist():
    findings = run_rule(make_shape([TextRun("가", None), TextRun("나", 10)]))
    assert sorted(severities(findings)) == ["unknown", "warning"]


@pytest.mark.parametrize(
    ("purpose", "size", "expected"),
    [
        (Purpose.PRESENTATION, 12, ["warning"]),
        (Purpose.REPORT, 12, []),
        (Purpose.REPORT, 11, ["review"]),
        (Purpose.REPORT, 9, ["warning"]),
    ],
)
def test_threshold_depends_on_purpose(purpose, size, expected):
    findings = run_rule(make_shape([TextRun("본문", size)]), policy=AnalysisPolicy(purpose=purpose))
    assert severities(findings) == expected


def test_slide_filter_limits_scope():
    small = [TextRun("각주", 10)]
    findings = run_rule(
        make_shape(small, slide_number=1),
        make_shape(small, slide_number=3),
        policy=AnalysisPolicy(slides=(3,)),
    )
    assert [f.shape_key for f in findings] == ["3:1"]


# --- 시각적 위계 (제목은 본문의 1.25배 이상) ---------------------------------


def test_title_well_above_body_has_no_hierarchy_finding():
    # 본문 20pt → 제목은 25pt 이상이면 된다. 32pt는 넉넉하다.
    assert hierarchy_findings(run_rule(title(32), body(20))) == []


def test_title_exactly_at_ratio_passes():
    # 20 * 1.25 = 25.0. 경계값은 통과여야 한다.
    assert hierarchy_findings(run_rule(title(25), body(20))) == []


def test_title_smaller_than_body_is_warning():
    findings = hierarchy_findings(run_rule(title(16), body(20)))
    assert [f.severity for f in findings] == ["warning"]
    finding = findings[0]
    assert finding.rule_id == "typography"
    assert finding.id == "typography:1:hierarchy"
    assert finding.shape_key == "1:1"  # 수정 대상은 제목 도형
    assert finding.measured == 16
    assert finding.expected == 25.0
    assert "제목 16pt / 본문 20pt" in finding.evidence


def test_title_equal_to_body_is_warning():
    findings = hierarchy_findings(run_rule(title(20), body(20)))
    assert [f.severity for f in findings] == ["warning"]


def test_title_larger_but_below_ratio_is_review():
    # 20 < 22 < 25 — 위계는 있지만 기준에 못 미친다.
    findings = hierarchy_findings(run_rule(title(22), body(20)))
    assert [f.severity for f in findings] == ["review"]
    assert findings[0].expected == 25.0


def test_fractional_threshold_is_not_rounded_for_judgment():
    findings = hierarchy_findings(run_rule(title(20), body(19)))
    assert findings[0].expected == 23.75
    assert hierarchy_findings(run_rule(title(26.25), body(21))) == []


def test_content_placeholder_counts_as_body():
    findings = hierarchy_findings(run_rule(title(20), body(20, kind=7)))
    assert findings[0].expected == 25


def test_plain_text_boxes_have_a_review_when_the_top_box_is_a_plausible_heading():
    from dataclasses import replace

    heading = replace(make_shape([TextRun("제목", 18)], shape_id=1), shape_type=17, box=Box(20, 20, 200, 40))
    content = replace(make_shape([TextRun("본문", 20)], shape_id=2), shape_type=17, box=Box(20, 110, 400, 60))
    findings = hierarchy_findings(run_rule(heading, content))
    assert [finding.severity for finding in findings] == ["review"]
    assert findings[0].rule_id == "typography.hierarchy.inferred"


def test_center_title_placeholder_counts_as_title():
    findings = hierarchy_findings(run_rule(title(16, kind=3), body(20)))
    assert [f.severity for f in findings] == ["warning"]


def test_vertical_placeholders_count():
    findings = hierarchy_findings(run_rule(title(16, kind=5), body(20, kind=6)))
    assert [f.severity for f in findings] == ["warning"]


def test_slide_without_body_is_not_checked():
    assert hierarchy_findings(run_rule(title(16))) == []


def test_slide_without_title_is_not_checked():
    assert hierarchy_findings(run_rule(body(20))) == []


def test_shapes_without_placeholder_type_are_not_checked():
    # OOXML 경로는 자리 표시자 종류를 주지 않는다. 억지로 짐작하지 않는다.
    plain_title = make_shape([TextRun("제목", 16)], shape_id=1)
    plain_body = make_shape([TextRun("본문", 20)], shape_id=2)
    assert hierarchy_findings(run_rule(plain_title, plain_body)) == []


def test_unsupported_shapes_are_not_checked():
    # 지원하지 않는 객체는 크기 검사에서 이미 unknown으로 보고된다.
    rough_title = make_shape([TextRun("제목", 16)], supported=False, placeholder_type=1)
    assert hierarchy_findings(run_rule(rough_title, body(20))) == []


def test_unknown_title_size_is_unknown():
    findings = hierarchy_findings(run_rule(title(None), body(20)))
    assert [f.severity for f in findings] == ["unknown"]
    finding = findings[0]
    assert finding.measured is None
    assert finding.expected is None
    assert "제목" in finding.evidence


def test_unknown_body_size_is_unknown():
    findings = hierarchy_findings(run_rule(title(16), body(None)))
    assert [f.severity for f in findings] == ["unknown"]
    assert "본문" in findings[0].evidence


def test_largest_body_run_is_the_comparison():
    # 본문이 여러 개면 제목과 겨루는 건 가장 큰 글자다.
    small = body(14, shape_id=2)
    large = body(24, shape_id=3)
    findings = hierarchy_findings(run_rule(title(26), small, large))
    assert [f.severity for f in findings] == ["review"]
    assert findings[0].expected == 30.0  # 24 * 1.25


def test_each_slide_is_checked_separately():
    findings = hierarchy_findings(
        run_rule(
            title(32, slide_number=1, shape_id=1),
            body(20, slide_number=1, shape_id=2),
            title(16, slide_number=2, shape_id=3),
            body(20, slide_number=2, shape_id=4),
        )
    )
    assert [f.id for f in findings] == ["typography:2:hierarchy"]
    assert findings[0].shape_key == "2:3"


def test_hidden_slides_are_skipped_for_hierarchy():
    policy = AnalysisPolicy(slides=(2,))
    findings = hierarchy_findings(
        run_rule(title(16, slide_number=1), body(20, slide_number=1), policy=policy)
    )
    assert findings == []
