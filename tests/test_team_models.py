"""팀 실제 모델(domain.models, domain.policy)과 연결되는지 확인하는 테스트.

검사 규칙(B)이 만드는 Finding 모양을 그대로 만들어 넣습니다.
  - rule_id: contrast / typography / layout.margin / layout.overflow / layout.overlap / layout.density
  - shape_key: "슬라이드:도형ID", severity: warning / review / unknown
  - measured / expected: 숫자 하나씩
"""

from pathlib import Path

import pytest

from slideguard.adapters.powerpoint.patcher import ApplyRejected, PowerPointPatcher, sha256_of
from slideguard.domain.models import (
    AnalysisReport,
    Box,
    DeckSnapshot,
    Finding,
    ObjectLocator,
    Shape,
    TextRun,
)
from slideguard.domain.policy import AnalysisPolicy, Purpose, Strength
from slideguard.planning import MinimumChangePlanner, Property, Rule, RuleBasedPlanner, TeamBindings
from slideguard.planning.contracts import ChangeProposal, Status
from tests.test_patcher import Doc, DocRun, DocShape, opener_for

# ---------------------------------------------------------------- 만들기 도우미


def text_shape(shape_id, box, size=24.0, color=(0, 0, 0), bold=False, **extra):
    return Shape(
        slide_number=1,
        shape_id=shape_id,
        name=f"글상자 {shape_id}",
        box=box,
        text="글자",
        runs=(TextRun("글자", font_size_pt=size, color=color, bold=bold),),
        background=(255, 255, 255),
        supported=True,
        **extra,
    )


def finding(rule_id, key, severity="warning", measured=None, expected=None, finding_id=None):
    return Finding(
        id=finding_id or f"{rule_id}:{key}",
        rule_id=rule_id,
        shape_key=key,
        severity=severity,
        message="",
        measured=measured,
        expected=expected,
        evidence="",
    )


def deck_of(*shapes, width=720.0, height=540.0, path="deck.pptx", sha="abc"):
    return DeckSnapshot(path, sha, 1, width, height, shapes=shapes)


# ---------------------------------------------------------------- 검사 결과 번역


def test_contrast_colors_come_from_snapshot():
    shape = text_shape(2, Box(60, 60, 400, 100), color=(200, 200, 200))
    info = TeamBindings().read_finding(
        finding("contrast", "1:2", measured=1.67, expected=4.5), deck_of(shape)
    )
    assert info.rule == Rule.CONTRAST
    assert (info.slide_index, info.shape_id) == (1, 2)
    assert info.values["foreground"] == "C8C8C8"
    assert info.values["background"] == "FFFFFF"
    assert info.values["required_ratio"] == 4.5
    assert info.finding_id == "contrast:1:2"


def test_contrast_without_matching_run_does_not_guess_colors():
    shape = text_shape(2, Box(60, 60, 400, 100), color=(0, 0, 0))
    info = TeamBindings().read_finding(
        finding("contrast", "1:2", measured=1.67, expected=4.5), deck_of(shape)
    )
    assert "foreground" not in info.values


def test_severity_names():
    bindings = TeamBindings()
    assert bindings.read_finding(finding("typography", "1:2", "warning")).status == Status.WARNING
    assert bindings.read_finding(finding("typography", "1:2", "review")).status == Status.REVIEW
    assert bindings.read_finding(finding("typography", "1:2", "unknown")).status == Status.UNMEASURABLE


def test_margin_rule_depends_on_severity():
    bindings = TeamBindings()
    assert bindings.read_finding(finding("layout.margin", "1:2", "warning")).rule == Rule.OFF_SLIDE
    assert bindings.read_finding(finding("layout.margin", "1:2", "review")).rule == Rule.SAFE_MARGIN


def test_overflow_needed_height_includes_inner_margins():
    shape = text_shape(
        3,
        Box(60, 200, 400, 100),
        rendered_box=Box(60, 195, 400, 140),
        content_box=Box(67.2, 207.2, 385.6, 85.6),
    )
    info = TeamBindings().read_finding(finding("layout.overflow", "1:3", measured=35.0), deck_of(shape))
    assert info.values["needed_height"] == pytest.approx(154.4)


def test_overflow_without_content_box_does_not_guess():
    shape = text_shape(3, Box(60, 200, 400, 100), rendered_box=Box(60, 195, 400, 140))
    info = TeamBindings().read_finding(finding("layout.overflow", "1:3", measured=35.0), deck_of(shape))
    assert "needed_height" not in info.values


@pytest.mark.parametrize(
    ("anchor", "finding_id", "other"),
    [
        ("1:2", "layout.overlap:1:2:1:12", 12),
        ("1:12", "layout.overlap:1:2:1:12", 2),
    ],
)
def test_overlap_partner_from_finding_id(anchor, finding_id, other):
    info = TeamBindings().read_finding(finding("layout.overlap", anchor, "review", finding_id=finding_id))
    assert info.values["other_shape_id"] == other


def test_density_line_count():
    f = finding("layout.density", "1:2", "review", measured=9, finding_id="layout.density:1:lines")
    assert TeamBindings().read_finding(f).values["line_count"] == 9


# ---------------------------------------------------------------- 스냅샷 번역


def test_slide_size_uses_real_deck_size():
    assert TeamBindings().slide_size(deck_of(width=720.0, height=540.0)) == (720.0, 540.0)


def test_read_shape_uses_box_and_rgb_tuple():
    info = TeamBindings().read_shape(text_shape(2, Box(10, 20, 300, 40), size=12, color=(255, 0, 0)))
    assert (info.left, info.top, info.width, info.height) == (10, 20, 300, 40)
    assert info.runs[0].size_pt == 12
    assert info.runs[0].color == "FF0000"
    assert info.kind == "text"


def test_group_child_is_not_patchable():
    child = text_shape(7, Box(10, 20, 300, 40), locator=ObjectLocator((5, 7)))
    deck = deck_of(child)
    bindings = TeamBindings()
    info = bindings.read_finding(finding("typography", child.key, measured=10.0, expected=14.0), deck)
    assert not bindings.can_patch(info)
    result = RuleBasedPlanner().plan(
        deck, [finding("typography", child.key, measured=10.0, expected=14.0)], AnalysisPolicy()
    )
    assert result.proposals == []
    assert "그룹" in result.skipped[0].reason


# ---------------------------------------------------------------- 정책 번역


@pytest.mark.parametrize(
    ("strength", "level"),
    [(Strength.LIGHT, "passive"), (Strength.STANDARD, "balanced"), (Strength.STRONG, "aggressive")],
)
def test_strength_names(strength, level):
    assert TeamBindings().strength(AnalysisPolicy(strength=strength)) == level


@pytest.mark.parametrize(
    ("purpose", "name"),
    [
        (Purpose.PRESENTATION, Purpose.PRESENTATION),
        (Purpose.LECTURE, Purpose.LECTURE),
        (Purpose.REPORT, Purpose.REPORT),
    ],
)
def test_purpose_names(purpose, name):
    assert TeamBindings().purpose(AnalysisPolicy(purpose=purpose)) == name


def test_locked_shape_key_is_respected():
    policy = AnalysisPolicy(locked_shapes=frozenset({"1:2"}))
    bindings = TeamBindings()
    assert bindings.is_locked(policy, 1, 2, Property.FONT_SIZE)
    assert not bindings.is_locked(policy, 1, 3, Property.FONT_SIZE)


def test_locked_property_is_respected():
    policy = AnalysisPolicy(locked_properties=frozenset({"font_size"}))
    bindings = TeamBindings()
    assert bindings.is_locked(policy, 1, 2, Property.FONT_SIZE)
    assert bindings.is_locked(policy, 1, 2, Property.AUTOFIT)
    assert not bindings.is_locked(policy, 1, 2, Property.FONT_COLOR)


def test_slides_outside_policy_scope_are_locked():
    policy = AnalysisPolicy(slides=(2,))
    assert TeamBindings().is_locked(policy, 1, 2, Property.FONT_COLOR)


# ---------------------------------------------------------------- 팀 인터페이스 (ports.py)


def small_gray_deck(path="deck.pptx", sha="abc"):
    return deck_of(text_shape(2, Box(60, 60, 400, 100), size=10.0, color=(200, 200, 200)), path=path, sha=sha)


def small_gray_findings():
    return [
        finding("typography", "1:2", measured=10.0, expected=14.0, finding_id="typography:1:2:1"),
        finding("contrast", "1:2", measured=1.67, expected=4.5, finding_id="contrast:1:2:1"),
    ]


def test_propose_returns_team_change_proposals():
    report = AnalysisReport(deck=small_gray_deck(), findings=small_gray_findings())
    planner = MinimumChangePlanner()
    proposals = planner.propose(report, AnalysisPolicy())

    by_property = {p.property: p for p in proposals}
    assert by_property[Property.FONT_SIZE].after == 14.0
    assert by_property[Property.FONT_SIZE].finding_id == "typography:1:2:1"
    assert by_property[Property.FONT_SIZE].shape_key == "1:2"
    assert by_property[Property.FONT_COLOR].before == "C8C8C8"
    assert len({p.id for p in proposals}) == len(proposals)
    assert planner.last_result is not None


def test_team_proposal_round_trip_keeps_group():
    bindings = TeamBindings()
    original = ChangeProposal(
        1, 3, Property.SHAPE_POSITION, (60.0, 200.0), (60.0, 260.0), 0.1, "overflow", group="g1"
    )
    team = bindings.to_team_proposal(original)
    back = bindings.from_team_proposal(team)
    assert team.id == "shape.position@1:3#g1"
    assert (back.slide_index, back.shape_id, back.prop, back.group) == (1, 3, Property.SHAPE_POSITION, "g1")
    assert back.before == (60.0, 200.0)


def test_apply_to_copy_applies_selected_proposals(tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"fake pptx bytes")
    deck = small_gray_deck(str(source), sha256_of(str(source)))
    report = AnalysisReport(deck=deck, findings=small_gray_findings())
    report.proposals = MinimumChangePlanner().propose(report, AnalysisPolicy())

    run = DocRun(10.0, "C8C8C8")
    doc = Doc(DocShape(runs=[run]))
    doc.shapes = {(1, 2): doc.shapes[(1, 5)]}
    patcher = PowerPointPatcher(opener_for(doc))
    size_id = next(p.id for p in report.proposals if p.property == Property.FONT_SIZE)

    output = patcher.apply_to_copy(report, [size_id], tmp_path / "out.pptx", AnalysisPolicy())

    assert output == Path(tmp_path / "out.pptx")
    assert run.size == 14.0
    assert run.color == "C8C8C8"  # 고르지 않은 후보는 적용하지 않음
    assert doc.saved


def test_apply_to_copy_rejects_unknown_id(tmp_path):
    report = AnalysisReport(deck=small_gray_deck(), findings=[])
    with pytest.raises(ApplyRejected):
        PowerPointPatcher(opener_for(Doc(DocShape()))).apply_to_copy(
            report, ["없는 후보"], tmp_path / "out.pptx", AnalysisPolicy()
        )
