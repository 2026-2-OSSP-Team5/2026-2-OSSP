"""C의 기준값이 모두 B(domain/policy.py)에서 오는지 확인."""

import pytest

from slideguard.domain import policy as team
from slideguard.domain.policy import Purpose
from slideguard.planning.standards import build_standards


@pytest.mark.parametrize("purpose", list(Purpose))
def test_every_criterion_matches_b_policy(purpose):
    s = build_standards(purpose)
    font = team.font_size_threshold(purpose)
    density = team.density_threshold(purpose)
    assert s.min_font_pt.value == font.minimum_pt
    assert s.recommended_font_pt.value == font.recommended_pt
    assert s.large_text_pt.value == team.LARGE_TEXT_PT
    assert s.large_bold_text_pt.value == team.LARGE_BOLD_TEXT_PT
    assert s.contrast_normal.value == team.CONTRAST_NORMAL
    assert s.contrast_large.value == team.CONTRAST_LARGE
    assert s.safe_margin_ratio.value == team.SAFE_MARGIN_RATIO
    assert s.max_lines.value == density.max_lines
    assert s.max_chars.value == density.max_chars


def test_criteria_missing_in_b_are_not_invented():
    s = build_standards(Purpose.PRESENTATION)
    assert s.min_line_spacing.value is None
    assert s.title_body_ratio.value == 1.25


def test_every_basis_names_b_as_source():
    for _name, criterion in build_standards(Purpose.LECTURE).criteria():
        assert criterion.basis.startswith("B")


def test_unknown_purpose_is_rejected():
    with pytest.raises(ValueError):
        build_standards("online")
