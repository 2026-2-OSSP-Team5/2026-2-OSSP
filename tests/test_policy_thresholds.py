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
    density_threshold,
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


def test_presentation_density_matches_the_measured_width_conversion():
    """발표용 글자 수는 6x6의 "한 줄 6단어"를 한국어 어절 폭으로 환산한 값이다.

    교수님 면담(2026-11-10) 제안에 따라, 글자 수에 전각·반각을 가정하지 않고
    같은 글꼴·같은 크기에서 두 언어의 실제 자폭을 쟀다.

    측정: AppleSDGothicNeo 18pt, 발표용·강의용·보고서용 슬라이드 117장
      한국어 어절  2,727개  평균 44.43px (2.94자)
      영어 단어      709개  평균 42.52px (4.87자)
      공백 한 칸            5.00px

    영어 6단어 한 줄과 같은 폭에 한국어는 5.8어절이 들어가고, 6줄이면 약 102자다.
    영어를 통념값 4.7자로 두고 계산하면 97자가 나와 두 방법이 5% 안에서 일치하므로
    그 사이인 100자로 두었다.
    """
    ko_px, en_px, space_px, ko_chars = 44.43, 42.52, 5.00, 2.94
    en_line_px = 6 * en_px + 5 * space_px
    per_line = (en_line_px + space_px) / (ko_px + space_px)
    measured_chars = per_line * 6 * ko_chars  # 약 102자

    limits = density_threshold(Purpose.PRESENTATION)
    assert abs(limits.max_chars - measured_chars) <= 5  # 영어 통념값으로 계산하면 97자
    assert limits.max_lines == 6  # 6x6 법칙


@pytest.mark.parametrize("purpose, multiple", [(Purpose.LECTURE, 1.5), (Purpose.REPORT, 3.0)])
def test_other_purposes_are_multiples_of_presentation(purpose, multiple):
    """강의·보고용은 발표용의 배수로 둔다.

    근거 자료도 팀 합의도 아직 없는 제안값이다. 값이 바뀌면 이 테스트도 함께 고친다.
    """
    base = density_threshold(Purpose.PRESENTATION)
    limits = density_threshold(purpose)
    assert limits.max_chars == round(base.max_chars * multiple)
    assert limits.max_lines == round(base.max_lines * multiple)
