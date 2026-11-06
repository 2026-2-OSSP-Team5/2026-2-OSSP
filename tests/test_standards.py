"""용도별 기준값과 사용자 설정 테스트."""

import json

import pytest

from slideguard.planning.standards import (
    build_standards,
    load_overrides,
    min_font_for,
    projected_contrast_requirement,
)


def test_presentation_preset_values_follow_formulas():
    s = build_standards("presentation")
    assert s.min_font_pt.value == 22.0  # 16분각, 거리비 6 → 21.5pt를 0.5pt 단위로 올림
    assert s.large_text_pt.value == 29.0
    assert s.large_bold_text_pt.value == 22.5
    assert s.contrast_normal.value == 6.0  # 투사 15:1 화면에서도 4.5:1 유지
    assert s.contrast_large.value == 3.9
    assert s.contrast_normal.kind == "계산"


def test_lecture_needs_bigger_text_than_presentation():
    assert build_standards("lecture").min_font_pt.value > build_standards("presentation").min_font_pt.value


def test_screen_purposes_use_wcag_as_is():
    s = build_standards("online")
    assert (s.contrast_normal.value, s.contrast_large.value) == (4.5, 3.0)
    assert s.contrast_normal.kind == "표준"


def test_every_value_has_a_basis():
    for purpose in ("presentation", "lecture", "online", "document"):
        for _name, criterion in build_standards(purpose).criteria():
            assert criterion.basis
            assert criterion.kind in ("표준", "계산", "가정", "사용자 지정")


def test_projected_contrast_formula():
    # 어두운 쪽이 완전 검정일 때 투사 명암비 = (L1 + e) / e  가 목표 이상이어야 함
    assert round(projected_contrast_requirement(4.5, 15), 2) == 6.0
    assert projected_contrast_requirement(4.5, 100) == 4.5  # 화면이 충분히 좋으면 WCAG 그대로
    assert projected_contrast_requirement(4.5, None) == 4.5


def test_min_font_grows_with_distance():
    assert min_font_for(8, 16, 0.7, 540) > min_font_for(6, 16, 0.7, 540)


def test_parameter_override_recomputes_values():
    s = build_standards("presentation", {"viewing_ratio": 8})
    assert s.parameters["viewing_ratio"].kind == "사용자 지정"
    assert s.min_font_pt.value > 22.0


def test_value_override_is_marked_and_loosening_is_warned():
    s = build_standards("presentation", {"min_font_pt": 18, "contrast_normal": 4.0})
    assert s.min_font_pt.value == 18
    assert s.min_font_pt.kind == "사용자 지정"
    assert any("WCAG AA" in w for w in s.warnings)
    assert any("최소 글자 크기" in w for w in s.warnings)


def test_stricter_override_has_no_warning():
    s = build_standards("presentation", {"min_font_pt": 30})
    assert s.warnings == []


def test_invalid_settings_are_rejected():
    with pytest.raises(ValueError):
        build_standards("unknown_purpose")
    with pytest.raises(ValueError):
        build_standards("presentation", {"font_minimum": 20})  # 없는 항목
    with pytest.raises(ValueError):
        build_standards("presentation", {"contrast_normal": 50})  # 범위 밖


def test_load_overrides_from_json(tmp_path):
    path = tmp_path / "my.json"
    path.write_text(json.dumps({"purpose": "lecture", "overrides": {"min_font_pt": 28}}), encoding="utf-8")
    data = load_overrides(str(path))
    s = build_standards(data["purpose"], data["overrides"])
    assert s.min_font_pt.value == 28
