import pytest

from slideguard.domain.policy import (
    CONTRAST_LARGE,
    CONTRAST_NORMAL,
    FONT_SIZE_THRESHOLDS,
    SAFE_MARGIN_RATIO,
    Purpose,
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
