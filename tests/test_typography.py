import pytest

from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy, Purpose


def make_shape(runs, supported=True, slide_number=1, shape_id=1, text=None, notes=()):
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
