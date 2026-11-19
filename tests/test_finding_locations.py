from dataclasses import replace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from slideguard.adapters.storage.sqlite import SqliteReportRepository
from slideguard.checks.contrast import ContrastRule
from slideguard.checks.layout import LayoutRule
from slideguard.checks.typography import TypographyRule
from slideguard.domain.models import AnalysisReport, Box, DeckSnapshot, Finding, ObjectLocator, Shape, TextRun
from slideguard.domain.policy import AnalysisPolicy
from slideguard.ui import overlay
from tests.test_overlay import _Collection, _SettingsRepository, _Tracker

CELL_BOX = Box(186, 165, 552, 86.4)
LEGACY_TEXT_BOX = Box(232.125, -16.8, 96.125, 33.6)
PIXEL_TEXT_BOX = Box(420, 195, 75, 27)


def legacy_report(path, visible_text_box=None):
    cell = Shape(
        10,
        4,
        "Table 3/table_cell/1,1",
        CELL_BOX,
        text="Header",
        runs=(TextRun("Header", None, (255, 255, 255)),),
        rendered_box=LEGACY_TEXT_BOX,
        visible_text_box=visible_text_box,
        locator=ObjectLocator((4,), "table_cell", (1, 1)),
        supported=True,
    )
    finding = Finding(
        "contrast:10:4:table_cell:1,1:1",
        "contrast",
        cell.key,
        "unknown",
        "Unknown background",
        None,
        None,
        "Unresolved table style",
        LEGACY_TEXT_BOX,
    )
    return AnalysisReport(
        deck=DeckSnapshot(str(path), "hash", 10, 1440, 810, shapes=(cell,)),
        findings=[finding],
        status="analyzed",
        policy_key='{"revision":1}',
    )


def test_rules_do_not_reuse_unverified_text_geometry_from_old_snapshots(tmp_path):
    report = legacy_report(tmp_path / "deck.pptx")
    policy = AnalysisPolicy()
    for rule in (ContrastRule(), TypographyRule(), LayoutRule()):
        findings = rule.evaluate(report.deck, policy)
        assert all(f.box == CELL_BOX for f in findings)
        assert not any(f.severity == "warning" for f in findings)


@pytest.mark.parametrize("pixels", [None, PIXEL_TEXT_BOX])
def test_locate_button_uses_safe_cell_coordinates_after_loading_old_history(tmp_path, monkeypatch, pixels):
    app = QApplication.instance() or QApplication([])
    repository = SqliteReportRepository(tmp_path / "history.sqlite3")
    path = tmp_path / "deck.pptx"
    repository.save(legacy_report(path, pixels))
    restored = repository.list_recent()[0].report
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: _SettingsRepository(tmp_path))
    monkeypatch.setattr(overlay, "build_report_repository", lambda: repository)
    monkeypatch.setattr(overlay, "build_analysis_policy", lambda *_args: AnalysisPolicy())
    monkeypatch.setattr(overlay.startup, "is_enabled", lambda: False)
    located = []

    def locate(_target, _path, _slide, box, **_kwargs):
        located.append(box)
        return QRect(300, 400, 100, 40)

    controller = overlay.OverlayController(
        tracker=_Tracker(overlay.PowerPointWindow(1, QRect(0, 0, 1200, 900), "deck.pptx - PowerPoint")),
        path_provider=lambda: path,
        collection_builder=lambda *_args, **_kwargs: _Collection(),
        outline_locator=locate,
    )
    try:
        controller.poll_powerpoint()
        controller.overlay.show_report(restored)
        assert controller.overlay.locate_button.isEnabled()
        controller.overlay.locate_button.click()
        assert located == [pixels or CELL_BOX]
        assert controller.outline.isVisible()
        assert app is QApplication.instance()
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()


def test_unknown_cell_location_does_not_fall_back_to_legacy_phantom_box(tmp_path):
    report = legacy_report(tmp_path / "deck.pptx")
    report.deck = replace(report.deck, shapes=(replace(report.deck.shapes[0], box=None),))
    assert overlay.finding_location_box(report, report.findings[0]) is None


def test_legacy_overlap_needs_reinspection_instead_of_relocating_an_invalid_intersection(tmp_path):
    report = legacy_report(tmp_path / "deck.pptx")
    finding = replace(report.findings[0], rule_id="layout.overlap")
    assert overlay.finding_location_box(report, finding) is None
    report.deck = replace(
        report.deck, shapes=(replace(report.deck.shapes[0], visible_text_box=PIXEL_TEXT_BOX),)
    )
    intersection = Box(425, 200, 20, 15)
    assert overlay.finding_location_box(report, replace(finding, box=intersection)) == intersection
