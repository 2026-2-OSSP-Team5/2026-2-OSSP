from dataclasses import replace

import pytest

from slideguard.adapters.storage.element_groups import SqliteElementGroupRepository
from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.domain.models import (
    AnalysisReport,
    Box,
    ChangeProposal,
    DeckSnapshot,
    ElementGroup,
    Finding,
    Shape,
    TextRun,
)
from slideguard.domain.policy import AnalysisPolicy, Strength
from slideguard.planning.element_groups import (
    align_group,
    detect_groups,
    edit_group,
    grouped_findings,
    proposal_options,
)


def page(number, x=870, y=480, **changes):
    shape = Shape(
        number,
        9,
        "쪽 번호",
        Box(x, y, 60, 24),
        text=str(number),
        runs=(TextRun(str(number), 10, (0, 0, 0)),),
        supported=True,
    )
    return replace(shape, **changes)


def deck(*shapes, path="deck.pptx"):
    count = max((s.slide_number for s in shapes), default=1)
    return DeckSnapshot(
        str(path),
        "hash",
        count,
        960,
        540,
        tuple(shapes),
        slide_fingerprints=tuple((str(255 + n), f"hash-{n}") for n in range(1, count + 1)),
    )


def report_with_group():
    snapshot = deck(page(1), page(2, x=850), page(3))
    findings = [Finding(f"f:{n}", "typography", f"{n}:9", "warning", "작은 글자", 10, 14, "") for n in (1, 2)]
    proposals = [
        ChangeProposal(f"font.size@{n}:9{suffix}", f"f:{n}", f"{n}:9", "font.size", 10, target, 0.1)
        for n in (1, 2)
        for suffix, target in (("", 14), ("~rec", 18))
    ]
    return AnalysisReport(snapshot, findings, proposals, element_groups=detect_groups(snapshot))


def test_detects_handwritten_page_numbers_even_when_one_position_is_wrong():
    [group] = detect_groups(deck(page(1), page(2, x=825, y=490), page(3)))
    assert group.title == "쪽 번호" and group.members == ("1:9", "2:9", "3:9")


def test_does_not_group_body_placeholders_or_large_unrelated_body_text():
    assert not detect_groups(deck(page(1, placeholder_type=2), page(2, placeholder_type=2)))
    assert not detect_groups(
        deck(
            page(1, box=Box(50, 100, 800, 250), text="본문 A"),
            page(2, box=Box(50, 100, 800, 250), text="본문 B"),
        )
    )


def test_edited_membership_survives_detection_and_a_shape_belongs_to_one_group():
    snapshot = deck(page(1), page(2), page(3), page(4, x=100))
    [auto] = detect_groups(snapshot)
    edited = edit_group([auto], replace(auto, members=("1:9", "3:9", "4:9")))
    [result] = detect_groups(snapshot, [g for g in edited if g.manual])
    assert result.members == ("1:9", "3:9", "4:9") and result.excluded == ("2:9",)
    new = ElementGroup("new", "다른 그룹", ("3:9", "2:9"), manual=True)
    groups = edit_group(edited, new)
    assert len([g for g in groups if "3:9" in g.members]) == 1


def test_group_actions_keep_variants_separate_and_do_not_invent_color_changes():
    report = report_with_group()
    color = ChangeProposal("font.color@1:9", "c", "1:9", "font.color", "000000", "AAAAAA", 0.1)
    report.proposals.append(color)
    actions = proposal_options(report, report.element_groups[0].members)
    assert len(actions) == 3
    assert {ids for ids, p, _, _ in actions if p.property == "font.size"} == {
        ("font.size@1:9", "font.size@2:9"),
        ("font.size@1:9~rec", "font.size@2:9~rec"),
    }
    assert next(ids for ids, p, _, _ in actions if p.property == "font.color") == (color.id,)


def test_group_actions_preserve_atomic_reflow_dependencies_without_duplicate_ids():
    report = report_with_group()
    report.proposals = [
        ChangeProposal("shape.height@1:9#flow", "f:1", "1:9", "shape.height", 24, 40, 0.1),
        ChangeProposal(
            "shape.position@1:10#flow", "f:1", "1:10", "shape.position", (50, 100), (50, 116), 0.1
        ),
    ]
    [(ids, _, _, _)] = proposal_options(report, ("1:9", "2:9"))
    assert ids == tuple(p.id for p in report.proposals)


def test_alignment_proposes_reference_coordinates_and_respects_locks_and_scope():
    snapshot = deck(page(1), page(2, x=830), page(3, x=845))
    [group] = detect_groups(snapshot)
    policy = AnalysisPolicy(locked_shapes=frozenset({"3:9"}))
    proposals, skipped = align_group(snapshot, group, "1:9", policy)
    assert [p.shape_key for p in proposals] == ["2:9"]
    assert proposals[0].before == (830, 480) and proposals[0].after == (870, 480)
    assert "고정" in skipped[0]
    assert align_group(snapshot, group, "1:9", AnalysisPolicy(slides=(1,)))[0] == []
    with pytest.raises(ValueError, match="표준"):
        align_group(snapshot, group, "1:9", AnalysisPolicy(strength=Strength.LIGHT))


def test_alignment_never_introduces_a_new_collision_or_leaves_its_card():
    obstacle = Shape(2, 10, "다른 글자", Box(910, 480, 30, 24), text="다른 글", supported=True)
    snapshot = deck(page(1), page(2, x=820), obstacle)
    group = ElementGroup("g", "쪽 번호", ("1:9", "2:9"))
    proposals, skipped = align_group(snapshot, group, "1:9", AnalysisPolicy())
    assert not proposals and "겹치" in skipped[0]
    holder = replace(obstacle, box=Box(810, 470, 80, 50), text="")
    assert not align_group(deck(page(1), page(2, x=820), holder), group, "1:9", AnalysisPolicy())[0]


def test_manual_groups_survive_restart_and_slide_reordering_but_not_reused_shape_identity(tmp_path):
    repository = SqliteElementGroupRepository(tmp_path / "settings.sqlite3")
    snapshot = deck(page(1), page(2), page(3), path=tmp_path / "deck.pptx")
    group = ElementGroup("g", "페이지", ("1:9", "3:9"), ("2:9",), True)
    repository.save(snapshot, [group])
    assert repository.load(snapshot) == [group]
    reordered = replace(snapshot, slide_fingerprints=tuple(reversed(snapshot.slide_fingerprints)))
    [restored] = repository.load(reordered)
    assert restored.members == ("3:9", "1:9") and restored.excluded == ("2:9",)
    changed = replace(snapshot, shapes=(page(1, text="새로운 객체"), page(2), page(3)))
    assert repository.load(changed)[0].members == ("3:9",)


def test_group_metadata_roundtrips_in_reports(tmp_path):
    repository = SqliteReportRepository(tmp_path / "reports.sqlite3")
    report = report_with_group()
    repository.save(report)
    assert repository.list_recent()[0].report.element_groups == report.element_groups


def test_group_rows_aggregate_local_problems_but_keep_slide_density_separate():
    report = report_with_group()
    density = Finding("density:1", "layout.density", "1:9", "review", "정보량", 110, 100, "")
    report.findings.append(density)
    rows = grouped_findings(report)
    assert len(rows) == 2 and len(rows[0][2]) == 2
    assert rows[1] == (density, None, [density])


def test_alignment_stays_separate_from_other_position_fixes_and_can_be_undone():
    from tests.test_live_edit import _editor
    from tests.test_patcher import DocShape

    report = report_with_group()
    [group] = report.element_groups
    alignments, _ = align_group(report.deck, group, "1:9", AnalysisPolicy())
    other = ChangeProposal("shape.position@2:9", "f", "2:9", "shape.position", (850, 480), (860, 480), 0.1)
    report.proposals = [other, *alignments]
    assert len(proposal_options(report, group.members)) == 2
    shape = DocShape(geometry=(850, 480, 60, 24))

    class Document:
        def find_shape(self, slide, shape_id):
            assert (slide, shape_id) == (2, 9)
            return shape

    editor = _editor(Document())
    ids = [p.id for p in alignments]
    assert editor.apply(report, ids).applied == ids
    assert tuple(shape.geometry[:2]) == (870, 480)
    editor.revert(ids)
    assert tuple(shape.geometry[:2]) == (850, 480)
