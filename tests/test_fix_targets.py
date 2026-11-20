"""수정 목표 파라미터(FixTargets)와 수정 품질 개선 테스트."""

from types import SimpleNamespace

import pytest

from slideguard.adapters.storage.sqlite import WorkspaceSettings
from slideguard.domain.policy import AnalysisPolicy, ContrastGoal, FixTargets, FontGoal
from slideguard.planning import MinimumChangePlanner, Property, RuleBasedPlanner
from slideguard.planning.bindings import is_recommended
from slideguard.planning.color import contrast_ratio
from tests.fakes import FakeFinding, FakeRun, FakeShape, FakeSnapshot


def _contrast_finding(fg, bg="FFFFFF", required=None):
    measured = {"foreground": fg, "background": bg, "ratio": contrast_ratio(fg, bg)}
    if required is not None:
        measured["required_ratio"] = required  # B가 그 글자에 적용한 기준 (큰 글자면 3:1)
    return FakeFinding("contrast", 1, 5, "warning", measured)


def _plan(shape, findings, **fix):
    policy = AnalysisPolicy(fix=FixTargets(**fix))
    return RuleBasedPlanner().plan(FakeSnapshot(shapes=[shape]), findings, policy)


def _colors(proposals):
    return [p.after for p in proposals if p.prop == Property.FONT_COLOR]


def _ratio(color):
    return contrast_ratio(color, "FFFFFF")


# ---------------------------------------------------------------- 최소 수정 + 권장 수정


@pytest.mark.parametrize(
    ("goal", "ratio", "at_least", "below"),
    [
        (ContrastGoal.ENHANCED, None, 7.0, 7.3),  # WCAG AAA
        (ContrastGoal.CUSTOM, 6.0, 6.0, 6.3),
    ],
)
def test_recommended_fix_reaches_the_goal_and_minimum_stays_at_threshold(goal, ratio, at_least, below):
    shape = FakeShape(1, 5, [FakeRun("글자", 12.0, "B8C2C0", False)])
    result = _plan(shape, [_contrast_finding("B8C2C0")], contrast_goal=goal, contrast_ratio=ratio)
    assert 4.5 <= _ratio(_colors(result.proposals)[0]) < 4.7  # 최소 수정: 기준선 바로 위
    assert at_least <= _ratio(_colors(result.recommended)[0]) < below  # 권장 수정: 목표까지, 필요 이상은 아님
    assert result.recommended[0].variant == "recommended"


def test_large_text_gets_a_comfortable_recommended_fix():
    shape = FakeShape(1, 5, [FakeRun("41%", 24.0, "B8C2C0", True)])
    result = _plan(shape, [_contrast_finding("B8C2C0", required=3.0)])  # 큰 글자: 검사 기준 3:1
    assert 3.0 <= _ratio(_colors(result.proposals)[0]) < 3.2  # 평가에서 "여전히 흐리다"던 값
    assert _ratio(_colors(result.recommended)[0]) >= 4.5  # 기본 권장 목표: 여유 있게


def test_no_recommended_fix_when_it_equals_the_minimum():
    shape = FakeShape(1, 5, [FakeRun("글자", 12.0, "B8C2C0", False)])
    result = _plan(shape, [_contrast_finding("B8C2C0")])  # 일반 글자: 기준과 '여유 있게'가 둘 다 4.5
    assert result.proposals and result.recommended == []


def test_both_goals_minimum_turns_recommended_off():
    shape = FakeShape(1, 5, [FakeRun("41%", 24.0, "B8C2C0", True)])
    result = _plan(
        shape,
        [_contrast_finding("B8C2C0", required=3.0)],
        contrast_goal=ContrastGoal.MINIMUM,
        font_goal=FontGoal.MINIMUM,
    )
    assert result.recommended == []


def test_goal_never_goes_below_the_check_threshold():
    targets = FixTargets(contrast_goal=ContrastGoal.CUSTOM, contrast_ratio=2.0)
    assert targets.contrast_target(4.5, large=False) == 4.5


def test_invalid_goal_values_are_rejected():
    with pytest.raises(ValueError):
        FixTargets(contrast_goal=ContrastGoal.CUSTOM)
    with pytest.raises(ValueError):
        FixTargets(contrast_ratio=30)


def test_font_size_minimum_and_recommended():
    shape = FakeShape(1, 5, [FakeRun("본문", 12.0, "000000", False)])
    finding = FakeFinding("font_size", 1, 5, "warning", {"size_pt": 12.0})
    result = _plan(shape, [finding])
    assert (result.proposals[0].after, result.recommended[0].after) == (14.0, 18.0)  # 발표용 최소 / 권장


def test_team_ids_mark_recommended_and_stay_unique():
    shape = FakeShape(1, 5, [FakeRun("41%", 24.0, "B8C2C0", True), FakeRun("주차", 24.0, "BFBFBF", False)])
    findings = [_contrast_finding("B8C2C0", required=3.0), _contrast_finding("BFBFBF", required=3.0)]
    snapshot = FakeSnapshot(shapes=[shape])
    planner = MinimumChangePlanner()
    report = SimpleNamespace(deck=snapshot, findings=findings)
    ids = [p.id for p in planner.propose(report, AnalysisPolicy())]
    assert len(ids) == len(set(ids)) == 4  # 회색 2개 × (최소, 권장)
    assert sum(is_recommended(item) for item in ids) == 2
    assert "font.color@1:5" in ids  # 처음 것은 지금까지와 같은 ID


# ---------------------------------------------------------------- 한 상자 안 두 가지 회색


def test_two_gray_shades_in_one_box_are_both_fixed():
    shape = FakeShape(
        1, 5, [FakeRun("41%", 24.0, "B8C2C0", True), FakeRun("주차 구역", 24.0, "BFBFBF", False)]
    )
    result = _plan(
        shape, [_contrast_finding("B8C2C0", required=3.0), _contrast_finding("BFBFBF", required=3.0)]
    )
    assert {p.before for p in result.proposals if p.prop == Property.FONT_COLOR} == {"B8C2C0", "BFBFBF"}


# ---------------------------------------------------------------- 설정 저장


def test_fix_targets_survive_settings_round_trip(tmp_path):
    settings = WorkspaceSettings(
        tmp_path,
        False,
        policy_options={"fix": {"contrast_goal": "enhanced", "font_goal": "recommended"}},
    )
    policy = settings.policy_for()
    assert policy.fix == FixTargets(ContrastGoal.ENHANCED, None, FontGoal.RECOMMENDED)
    assert (
        policy.cache_key != AnalysisPolicy().cache_key
    )  # 기본값(여유 있게)과 다름  # 목표가 다르면 저장된 결과를 다시 쓰지 않음


# ---------------------------------------------------------------- 뼈대 (구현 예정)


@pytest.mark.skip(reason="TODO(C): planning/extras.unify_slide_scrims")
def test_scrims_on_one_slide_share_one_look():
    """같은 슬라이드 사진 위 글상자 3개 → 배경판 색·투명도가 하나로 통일."""


@pytest.mark.skip(reason="TODO(C): planning/extras.box_height_for_font")
def test_growing_font_also_grows_the_box():
    """12→18pt 후보와 함께 같은 group의 shape.height 후보가 생기고, 재검사에서 넘침이 없어야 함."""


@pytest.mark.skip(reason="TODO(B·C·A): 양쪽 정렬 검사 + Property.ALIGNMENT")
def test_justified_text_is_left_aligned():
    """WCAG 1.4.8: 양쪽 정렬 문단 → 왼쪽 정렬 후보."""


# ---------------------------------------------------------------- 패널: 최소/권장 고르기


@pytest.fixture(scope="module")
def application():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _variant_report(path):
    from slideguard.domain.models import AnalysisReport, ChangeProposal, DeckSnapshot, Finding

    findings = [
        Finding("contrast:1:5:1", "contrast", "1:5", "warning", "명암비", 1.8, 3.0, "", None),
        Finding("contrast:1:5:2", "contrast", "1:5", "warning", "명암비", 1.8, 3.0, "", None),
    ]
    proposals = [
        ChangeProposal("font.color@1:5", findings[0].id, "1:5", "font.color", "B8C2C0", "889895", 0.1),
        ChangeProposal("font.color@1:5~BFBFBF", findings[1].id, "1:5", "font.color", "BFBFBF", "949494", 0.1),
        ChangeProposal("font.color@1:5~rec", findings[0].id, "1:5", "font.color", "B8C2C0", "697A77", 0.2),
        ChangeProposal(
            "font.color@1:5~rec~BFBFBF", findings[1].id, "1:5", "font.color", "BFBFBF", "767676", 0.2
        ),
    ]
    deck = DeckSnapshot(str(path), "abc", 1, 960, 540)
    return AnalysisReport(deck=deck, findings=findings, proposals=proposals, status="analyzed")


def test_panel_offers_minimum_and_recommended_and_keeps_one_per_target(application, tmp_path):
    from PySide6.QtCore import Qt

    from slideguard.ui import overlay

    widget = overlay.OverlayWidget()
    try:
        report = _variant_report(tmp_path / "deck.pptx")
        widget.show_report(report)
        labels = [widget.proposals_list.item(i).text() for i in range(widget.proposals_list.count())]
        assert sum("최소 수정" in label for label in labels) == 2
        assert sum("권장 수정" in label for label in labels) == 2

        widget._apply_all_proposals(recommended=True)  # 권장 수정 전체
        assert set(widget.selected_proposal_ids()) == {"font.color@1:5~rec", "font.color@1:5~rec~BFBFBF"}
        assert (
            overlay._selection_conflicts(report, [p for p in report.proposals if is_recommended(p.id)]) == []
        )

        widget.proposal_items["font.color@1:5"].setCheckState(Qt.CheckState.Checked)  # 한 줄만 최소로 바꿈
        assert set(widget.selected_proposal_ids()) == {"font.color@1:5", "font.color@1:5~rec~BFBFBF"}

        widget._apply_all_proposals(recommended=False)  # 최소 수정 전체
        assert set(widget.selected_proposal_ids()) == {"font.color@1:5", "font.color@1:5~BFBFBF"}
    finally:
        widget.close()
