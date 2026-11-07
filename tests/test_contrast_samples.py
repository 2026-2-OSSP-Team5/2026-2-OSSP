"""배경색을 하나로 정할 수 없을 때 렌더링 픽셀로 근사 판정 (B의 명암비 규칙)."""

from slideguard.checks.contrast import ContrastRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import CONTRAST_NORMAL, AnalysisPolicy

BLACK, WHITE = (0, 0, 0), (255, 255, 255)


def samples(*colors):
    return tuple((float(i), 0.0, *color) for i, color in enumerate(colors))


def evaluate(runs, background_samples=()):
    shape = Shape(
        1,
        1,
        "사진 위 글자",
        Box(0, 0, 200, 50),
        text="".join(run.text for run in runs),
        runs=tuple(runs),
        background=None,
        supported=True,
        background_samples=background_samples,
    )
    deck = DeckSnapshot("test.pptx", "0" * 64, 1, 960, 540, shapes=(shape,))
    return ContrastRule().evaluate(deck, AnalysisPolicy())


def test_all_sampled_pixels_passing_is_a_pass():
    findings = evaluate(
        [TextRun("본문", 20, BLACK, False)], samples(WHITE, (240, 240, 240), (230, 250, 255), WHITE)
    )
    assert findings == []


def test_one_dark_pixel_makes_review_with_worst_ratio():
    findings = evaluate([TextRun("본문", 12, BLACK, False)], samples(WHITE, WHITE, (60, 60, 60), WHITE))
    assert [f.severity for f in findings] == ["review"]
    assert findings[0].measured < CONTRAST_NORMAL and findings[0].expected == CONTRAST_NORMAL
    assert "근사" in findings[0].evidence and "#3C3C3C" in findings[0].evidence


def test_large_text_uses_large_threshold():
    mid_gray = (118, 118, 118)  # 흰 글자 대비 약 4.5:1 미만, 3:1 이상
    findings = evaluate([TextRun("제목", 32, WHITE, False)], samples(mid_gray, mid_gray, mid_gray, mid_gray))
    assert findings == []


def test_without_enough_samples_stays_unknown():
    findings = evaluate([TextRun("본문", 20, BLACK, False)], samples(WHITE, WHITE))
    assert [f.severity for f in findings] == ["unknown"]


def test_unknown_text_color_stays_unknown_with_samples():
    findings = evaluate([TextRun("본문", 20, None, False)], samples(WHITE, WHITE, WHITE, WHITE))
    assert [f.severity for f in findings] == ["unknown"]


def test_unknown_background_shows_extraction_reason():
    shape = Shape(
        1,
        1,
        "s",
        Box(0, 0, 200, 50),
        text="본문",
        runs=(TextRun("본문", 20, BLACK, False),),
        background=None,
        supported=True,
        notes=("Background is unknown: object 50 is drawn over the text",),
    )
    deck = DeckSnapshot("test.pptx", "0" * 64, 1, 960, 540, shapes=(shape,))
    findings = ContrastRule().evaluate(deck, AnalysisPolicy())
    assert [f.severity for f in findings] == ["unknown"]
    assert "object 50 is drawn over the text" in findings[0].evidence
