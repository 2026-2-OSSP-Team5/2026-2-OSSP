import pytest

from slideguard.checks.contrast import (
    ContrastRule,
    contrast_ratio,
    large_text_state,
    relative_luminance,
)
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GRAY_777 = (119, 119, 119)  # 흰 배경 대비 약 4.48:1 (일반 텍스트 기준 4.5:1 바로 아래)
GRAY_949494 = (148, 148, 148)  # 흰 배경 대비 약 3.03:1 (대형 텍스트만 통과)


def make_shape(runs, background=WHITE, supported=True, slide_number=1, shape_id=1, text=None, notes=()):
    run_tuple = tuple(runs)
    return Shape(
        slide_number=slide_number,
        shape_id=shape_id,
        name=f"shape{shape_id}",
        box=Box(0, 0, 100, 50),
        text="".join(r.text for r in run_tuple) if text is None else text,
        runs=run_tuple,
        background=background,
        supported=supported,
        notes=tuple(notes),
    )


def make_deck(*shapes):
    return DeckSnapshot(
        source_path="test.pptx",
        source_sha256="0" * 64,
        slide_count=3,
        width_pt=960,
        height_pt=540,
        shapes=tuple(shapes),
    )


def run_rule(*shapes, policy=None):
    return ContrastRule().evaluate(make_deck(*shapes), policy or AnalysisPolicy())


# --- 계산 함수 ---------------------------------------------------------------
def test_luminance_extremes():
    assert relative_luminance(BLACK) == 0.0
    assert relative_luminance(WHITE) == pytest.approx(1.0)


def test_contrast_black_on_white_is_21():
    assert contrast_ratio(BLACK, WHITE) == pytest.approx(21.0)


def test_contrast_same_color_is_1():
    assert contrast_ratio(GRAY_777, GRAY_777) == pytest.approx(1.0)


def test_contrast_is_symmetric():
    assert contrast_ratio(GRAY_777, WHITE) == contrast_ratio(WHITE, GRAY_777)


def test_contrast_known_gray_value():
    assert contrast_ratio(GRAY_777, WHITE) == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize(
    ("size", "bold", "expected"),
    [
        (18, False, True),
        (24, None, True),
        (14, True, True),
        (16, True, True),
        (16, False, False),
        (13.9, True, False),
        (12, None, False),
        (16, None, None),  # 굵기를 알 수 없는 14~18pt
        (None, True, None),
    ],
)
def test_large_text_state(size, bold, expected):
    assert large_text_state(size, bold) is expected


# --- 규칙 --------------------------------------------------------------------
def test_good_contrast_has_no_findings():
    assert run_rule(make_shape([TextRun("안녕", 20, BLACK, False)])) == []


def test_low_contrast_normal_text_is_warning():
    findings = run_rule(make_shape([TextRun("본문", 12, GRAY_777, False)]))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warning"
    assert finding.rule_id == "contrast"
    assert finding.shape_key == "1:1"
    assert finding.measured == pytest.approx(4.48, abs=0.01)
    assert finding.expected == 4.5
    assert "#777777" in finding.evidence and "#FFFFFF" in finding.evidence


def test_large_text_passes_at_3_to_1():
    assert run_rule(make_shape([TextRun("제목", 24, GRAY_949494, False)])) == []


def test_normal_text_fails_at_same_ratio_as_large_pass():
    findings = run_rule(make_shape([TextRun("본문", 12, GRAY_949494, False)]))
    assert [f.severity for f in findings] == ["warning"]
    assert findings[0].expected == 4.5


def test_ratio_below_3_fails_even_for_large_text():
    findings = run_rule(make_shape([TextRun("제목", 32, (200, 200, 200), False)]))
    assert [f.severity for f in findings] == ["warning"]
    assert findings[0].expected == 3.0


def test_unknown_background_is_unknown_not_pass():
    findings = run_rule(make_shape([TextRun("본문", 20, BLACK, False)], background=None))
    assert [f.severity for f in findings] == ["unknown"]
    assert findings[0].measured is None


def test_unknown_text_color_is_unknown():
    findings = run_rule(make_shape([TextRun("본문", 20, None, False)]))
    assert [f.severity for f in findings] == ["unknown"]


def test_unknown_size_between_thresholds_is_unknown():
    findings = run_rule(make_shape([TextRun("본문", None, GRAY_949494, False)]))
    assert [f.severity for f in findings] == ["unknown"]


def test_unknown_bold_in_ambiguous_size_range_is_unknown():
    findings = run_rule(make_shape([TextRun("본문", 16, GRAY_949494, None)]))
    assert [f.severity for f in findings] == ["unknown"]


def test_unsupported_shape_is_unknown_with_notes():
    findings = run_rule(make_shape([TextRun("본문", 20, BLACK)], supported=False, notes=("그라데이션 배경",)))
    assert [f.severity for f in findings] == ["unknown"]
    assert "그라데이션" in findings[0].evidence


def test_shape_without_text_is_skipped():
    assert run_rule(make_shape([], text="", supported=False)) == []
    assert run_rule(make_shape([TextRun("   ", 12, GRAY_777)], text="  ")) == []


def test_duplicate_runs_produce_single_finding():
    runs = [TextRun("가", 12, GRAY_777, False), TextRun("나", 12, GRAY_777, False)]
    assert len(run_rule(make_shape(runs))) == 1


def test_different_runs_each_reported_with_unique_ids():
    runs = [TextRun("가", 12, GRAY_777, False), TextRun("나", 12, (200, 200, 200), False)]
    findings = run_rule(make_shape(runs))
    assert len(findings) == 2
    assert len({f.id for f in findings}) == 2


def test_slide_filter_limits_scope():
    bad = [TextRun("본문", 12, GRAY_777, False)]
    shapes = (make_shape(bad, slide_number=1), make_shape(bad, slide_number=2))
    findings = run_rule(*shapes, policy=AnalysisPolicy(slides=(2,)))
    assert [f.shape_key for f in findings] == ["2:1"]


def test_unknown_text_kind_below_three_is_warning_citing_both_targets():
    """글자 유형을 모르면 '대형'이라고 단정하지 않고, 두 기준 미달을 근거로 남긴다."""
    findings = run_rule(make_shape([TextRun("본문", None, (210, 210, 210), None)]))
    assert [f.severity for f in findings] == ["warning"]
    finding = findings[0]
    assert finding.expected == 4.5
    assert "대형 텍스트" not in finding.evidence
    assert "유형 미확정" in finding.evidence
    assert "4.5:1" in finding.evidence and "3.0:1" in finding.evidence
