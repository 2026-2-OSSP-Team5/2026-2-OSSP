from dataclasses import replace

from slideguard.checks.readability import ReadabilityRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun, TextSpan
from slideguard.domain.policy import AnalysisPolicy, FixTargets


def _shape(text="본문 설명을 읽기 쉽게 배치합니다. " * 5, **changes):
    base = Shape(
        2,
        3,
        "본문",
        Box(50, 100, 750, 300),
        text,
        (TextRun(text, 18, italic=False, underline=False),),
        supported=True,
        spans=(
            *(
                TextSpan("line", i + 1, text[start : start + 30], Box(50, 100 + i * 20, 750, 20))
                for i, start in enumerate(range(0, len(text), 30))
            ),
            TextSpan(
                "paragraph",
                1,
                text,
                Box(50, 100, 750, 100),
                (("Size", 18), ("SpaceWithin", 1.0), ("LineRuleWithin", -1), ("Alignment", 2)),
            ),
        ),
    )
    return replace(base, **changes)


def _evaluate(*shapes, policy=None):
    return ReadabilityRule().evaluate(
        DeckSnapshot("deck.pptx", "hash", 2, 960, 540, shapes), policy or AnalysisPolicy()
    )


def test_spacing_and_alignment_use_measured_paragraph_metadata():
    result = _evaluate(_shape())
    assert {f.rule_id for f in result} == {"readability.spacing.1", "readability.alignment.1"}
    assert all(f.severity == "review" for f in result)
    assert next(f for f in result if ".spacing." in f.rule_id).measured == 1.0
    assert not any(
        "spacing" in f.rule_id
        for f in _evaluate(_shape(), policy=AnalysisPolicy(fix=FixTargets(line_spacing=1)))
    )


def test_missing_metadata_never_implies_bad_spacing_or_alignment():
    shape = _shape(spans=(TextSpan("paragraph", 1, "x" * 100),))
    assert _evaluate(shape) == []


def test_fixed_point_spacing_is_normalized_by_actual_font_size():
    paragraph = TextSpan(
        "paragraph",
        1,
        "가" * 60,
        properties=(("Size", 20), ("SpaceWithin", 20), ("LineRuleWithin", 0), ("Start", 1), ("Length", 60)),
    )
    lines = tuple(
        TextSpan("line", i + 1, "가" * 30, properties=(("Start", i * 30 + 1), ("Length", 30)))
        for i in range(2)
    )
    result = _evaluate(_shape(spans=(paragraph, *lines)))
    assert len(result) == 1 and result[0].measured == 1.0


def test_one_line_paragraph_does_not_include_next_blank_line_at_rounded_boundary():
    # Actual PowerPoint values from 자바게티 7:11: the blank line starts
    # 0.0000076pt before the previous paragraph's reported bottom.
    paragraph = TextSpan(
        "paragraph",
        1,
        "책의 상태를 미리 알고 예약\r",
        Box(799.2, 270.0677185, 397.25, 38.4000015),
        (("Size", 32), ("SpaceWithin", 1), ("LineRuleWithin", -1), ("Start", 1), ("Length", 16)),
    )
    lines = (
        TextSpan("line", 1, paragraph.text, paragraph.box, (("Start", 1), ("Length", 16))),
        TextSpan("line", 2, "\r", Box(799.2, 308.4677124, 11.25, 38.4), (("Start", 17), ("Length", 1))),
        TextSpan(
            "line",
            3,
            "도서관 이용 만족도 상승",
            Box(799.2, 346.86, 343.75, 38.4),
            (("Start", 18), ("Length", 13)),
        ),
    )
    assert not any("spacing" in f.rule_id for f in _evaluate(_shape(spans=(paragraph, *lines))))
    # Old snapshots with no offsets must also ignore the blank line.
    old = tuple(replace(s, properties=()) if s.kind == "line" else s for s in (paragraph, *lines))
    assert not any("spacing" in f.rule_id for f in _evaluate(_shape(spans=old)))


def test_long_single_line_or_missing_line_measurements_never_imply_multiline_spacing():
    shape = _shape()
    paragraph = next(s for s in shape.spans if s.kind == "paragraph")
    for lines in ((), (TextSpan("line", 1, paragraph.text, paragraph.box),)):
        result = _evaluate(replace(shape, spans=(paragraph, *lines)))
        assert not any("spacing" in f.rule_id or "alignment" in f.rule_id for f in result)


def test_lines_from_another_column_cannot_join_a_paragraph_by_y_position():
    shape = _shape()
    paragraph = next(s for s in shape.spans if s.kind == "paragraph")
    own = TextSpan("line", 1, "본문", Box(50, 100, 200, 20))
    other = TextSpan("line", 2, "다른 칸", Box(900, 120, 200, 20))
    assert not _evaluate(replace(shape, spans=(paragraph, own, other)))


def test_readability_does_not_duplicate_layout_line_length_findings():
    for value in ("가" * 41, "a" * 81, "가" * 25 + "a" * 20):
        shape = _shape(value, spans=(TextSpan("line", 1, value),))
        assert not any(f.rule_id == "readability.line_length" for f in _evaluate(shape))


def test_emphasis_requires_complete_positive_measurement():
    value = "긴 본문 " * 20
    assert any(
        f.rule_id == "readability.italic"
        for f in _evaluate(_shape(value, spans=(), runs=(TextRun(value, 18, italic=True),)))
    )
    assert not _evaluate(_shape(value, spans=(), runs=(TextRun(value, 18),)))
    mixed = (TextRun(value[:50], 18, italic=True), TextRun(value[50:], 18))
    assert not _evaluate(_shape(value, spans=(), runs=mixed))


def test_graphic_contrast_requires_verified_background_and_meaning():
    shape = _shape(
        "",
        runs=(),
        spans=(),
        properties=(("meaningful_graphic_color", "AAAAAA"),),
        background=(255, 255, 255),
    )
    assert _evaluate(shape)[0].rule_id == "readability.graphic_contrast"
    assert _evaluate(replace(shape, background=None)) == []
    assert _evaluate(replace(shape, properties=())) == []


def test_color_only_chart_and_editorial_title_are_review_suggestions():
    chart = _shape("", spans=(), runs=(), shape_type=3, properties=(("chart_color_only", True),))
    title = _shape("결과", shape_id=2, placeholder_type=1, spans=())
    ids = {f.rule_id for f in _evaluate(chart, title)}
    assert ids == {"readability.color_only", "readability.message"}


def test_captions_report_distance_only_when_a_related_object_is_available():
    caption = _shape("그림 1. 설명", spans=(), box=Box(50, 50, 100, 20))
    picture = _shape("", shape_id=4, spans=(), runs=(), shape_type=13, box=Box(50, 400, 100, 100))
    assert any(f.rule_id == "readability.caption_distance" for f in _evaluate(caption, picture))
    assert _evaluate(caption) == []


def test_hidden_shapes_are_not_checked():
    assert _evaluate(_shape(effective_visible=False)) == []
