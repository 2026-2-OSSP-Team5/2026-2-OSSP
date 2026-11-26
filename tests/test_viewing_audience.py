"""환경 맞춤(화면 크기·거리)과 청중 맞춤 기준."""

import pytest

from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import Box, DeckSnapshot, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy, Audience, ThresholdOverrides, ViewingSetup


def _deck(size):
    shape = Shape(
        1, 2, "본문", Box(60, 120, 800, 200), text="본문", runs=(TextRun("본문", size),), supported=True
    )
    return DeckSnapshot("d.pptx", "0" * 64, 1, 960, 540, shapes=(shape,))


def test_viewing_distance_turns_into_a_minimum_size():
    """100인치 16:9 화면, 맨 뒷줄 10m: 글자 높이 0.2°가 되려면 16:9 슬라이드에서 약 30pt."""
    setup = ViewingSetup(100, 10)
    assert setup.minimum_pt(960, 540, 0.2) == pytest.approx(30.3, abs=0.1)
    # 가까우면 기준이 내려가지만 기본 기준(14pt)보다 낮추지는 않음
    near = AnalysisPolicy(viewing=ViewingSetup(65, 2))
    threshold, notes = near.slide_font_limits(960, 540)
    assert threshold.minimum_pt == 14.0 and notes == ""


def test_far_back_row_flags_text_that_passes_the_default_rule():
    far = AnalysisPolicy(viewing=ViewingSetup(100, 10))
    [finding] = TypographyRule().evaluate(_deck(24.0), far)
    assert finding.severity == "warning" and finding.expected == pytest.approx(30.3, abs=0.1)
    assert "10m" in finding.evidence
    assert TypographyRule().evaluate(_deck(24.0), AnalysisPolicy()) == []


def test_viewing_needs_both_values_and_sane_ranges():
    assert not ViewingSetup(100, None).active
    with pytest.raises(ValueError):
        ViewingSetup(5, 10)
    with pytest.raises(ValueError):
        ViewingSetup(100, 0)


def test_children_get_bigger_text_and_less_per_slide_unless_overridden():
    children = AnalysisPolicy(audience=Audience.CHILDREN)
    assert children.font_limits.minimum_pt == 17.5 and children.font_limits.recommended_pt == 22.5
    assert children.density_limits.max_lines == 4  # 6줄 × 0.7
    custom = AnalysisPolicy(audience=Audience.CHILDREN, thresholds=ThresholdOverrides(minimum_font=12))
    assert custom.font_limits.minimum_pt == 12


def test_new_options_change_the_cache_key_and_survive_strings():
    assert AnalysisPolicy().cache_key != AnalysisPolicy(audience="older").cache_key
    assert AnalysisPolicy(audience="older").audience == Audience.OLDER
    assert AnalysisPolicy().cache_key != AnalysisPolicy(color_vision="red_green").cache_key


def test_new_options_are_saved_with_the_workspace(tmp_path):
    from slideguard.adapters.storage.sqlite import SqliteWorkspaceSettingsRepository

    repository = SqliteWorkspaceSettingsRepository(tmp_path / "settings.sqlite3")
    policy = AnalysisPolicy(
        audience=Audience.CHILDREN, viewing=ViewingSetup(80, 8), color_vision="tritan", consistency=False
    )
    repository.save(tmp_path, recursive=False, policy=policy)
    restored = SqliteWorkspaceSettingsRepository(tmp_path / "settings.sqlite3").load().policy_for()
    assert restored.audience == Audience.CHILDREN and restored.viewing == ViewingSetup(80, 8)
    assert restored.color_vision == "tritan" and restored.consistency is False


def test_old_saved_on_off_colour_vision_setting_starts_as_none():
    """예전 버전은 색약 검사를 True(기본)로 저장했다 → 새 버전에서는 '고려 안 함'으로 시작."""
    from slideguard.adapters.storage.sqlite import _color_vision_option

    assert _color_vision_option(True) == "none" and _color_vision_option("deutan") == "deutan"
    assert _color_vision_option("weird") == "none" and _color_vision_option(None) == "none"
