import pytest

from slideguard.checks.contrast import ContrastRule
from slideguard.checks.layout import LayoutRule
from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import (
    CONTRAST_LARGE,
    CONTRAST_NORMAL,
    FONT_SIZE_THRESHOLDS,
    SAFE_MARGIN_RATIO,
    AnalysisPolicy,
    Purpose,
    ThresholdOverrides,
    font_size_threshold,
)


def test_wcag_contrast_thresholds():
    assert CONTRAST_NORMAL == 4.5
    assert CONTRAST_LARGE == 3.0


def test_safe_margin_is_five_percent():
    assert SAFE_MARGIN_RATIO == 0.05


def test_presentation_thresholds_match_agreed_criteria():
    threshold = font_size_threshold(Purpose.PRESENTATION)
    assert threshold.recommended_pt == 18.0
    assert threshold.minimum_pt == 14.0


def test_every_purpose_has_threshold():
    assert set(FONT_SIZE_THRESHOLDS) == set(Purpose)


@pytest.mark.parametrize("purpose", list(Purpose))
def test_minimum_not_above_recommended(purpose):
    threshold = font_size_threshold(purpose)
    assert 0 < threshold.minimum_pt <= threshold.recommended_pt


def test_custom_thresholds_reach_each_rule():
    text = "first line\nsecond line"
    shape = Shape(
        1,
        2,
        "Text",
        Box(30, 80, 300, 50),
        text,
        (TextRun(text, 16, (100, 100, 100), False),),
        background=(255, 255, 255),
        supported=True,
    )
    deck = DeckSnapshot("sample.pptx", "sample", 1, 960, 540, (shape,))
    defaults = AnalysisPolicy()
    custom = AnalysisPolicy(
        thresholds=ThresholdOverrides(
            minimum_font=17,
            recommended_font=19,
            max_chars=3,
            max_lines=1,
            contrast_normal=7,
            margin_percent=0,
        )
    )

    assert ContrastRule().evaluate(deck, defaults) == []
    assert ContrastRule().evaluate(deck, custom)[0].expected == 7
    assert TypographyRule().evaluate(deck, custom)[0].severity == "warning"
    assert TypographyRule().evaluate(deck, custom)[0].expected == 17
    assert any(f.rule_id == "layout.margin" for f in LayoutRule().evaluate(deck, defaults))
    layout = LayoutRule().evaluate(deck, custom)
    assert all(f.rule_id != "layout.margin" for f in layout)
    assert {f.expected for f in layout if f.rule_id == "layout.density"} == {1, 3}
