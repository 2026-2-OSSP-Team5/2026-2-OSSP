"""슬라이드 렌즈(검사 결과를 슬라이드 위에 겹쳐 그리는 오버레이) 테스트. PowerPoint 없이 확인한다."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect, QRectF, QSizeF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from slideguard.adapters.storage.sqlite import WorkspaceSettings
from slideguard.domain.models import AnalysisReport, Box, ChangeProposal, DeckSnapshot, Finding
from slideguard.ui import overlay
from slideguard.ui.slide_lens import (
    LensMark,
    SlideView,
    build_marks,
    map_box,
    metric_text,
    paint_lens,
    place_badges,
    summary_counts,
)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


DEFAULT_BOX = Box(100, 100, 200, 50)


def _finding(fid, rule, key, severity, measured, expected, box=DEFAULT_BOX):
    return Finding(fid, rule, key, severity, "메시지", measured, expected, "근거", box)


def _report(path: Path) -> AnalysisReport:
    findings = [
        _finding("typography:1:2:1", "typography", "1:2", "warning", 11.0, 14.0),
        _finding("layout.overlap:1:2:1:3", "layout.overlap", "1:2", "review", 30.0, 0.0),
        _finding("contrast:1:3", "contrast", "1:3", "warning", 1.8, 4.5, Box(100, 300, 300, 40)),
        _finding("layout.margin:1:4", "layout.margin", "1:4", "warning", -4.0, 36.0, Box(900, 20, 100, 40)),
        _finding("contrast:1:5", "contrast", "1:5", "unknown", None, None, None),
        _finding("typography:2:2:1", "typography", "2:2", "warning", 10.0, 14.0),
    ]
    proposals = [
        ChangeProposal("font.size@1:2", findings[0].id, "1:2", "font.size", 11.0, 14.0, 0.1),
        ChangeProposal("font.color@1:3", findings[2].id, "1:3", "font.color", "C8C8C8", "767676", 0.1),
    ]
    deck = DeckSnapshot(str(path), "abc", 2, 960, 540)
    return AnalysisReport(deck=deck, findings=findings, proposals=proposals, status="analyzed")


def test_marks_group_findings_per_object_and_keep_the_worst(tmp_path):
    marks = {mark.key: mark for mark in build_marks(_report(tmp_path / "d.pptx"), 1)}
    assert set(marks) == {"1:2", "1:3", "1:4", "1:5"}  # 2번 슬라이드 것은 빠짐
    assert marks["1:2"].kind == "warning" and marks["1:2"].value == "11pt" and marks["1:2"].more == 1
    assert marks["1:2"].target == "최소 14pt"
    assert marks["1:3"].value == "1.8:1"
    assert marks["1:4"].value == "슬라이드 밖"
    assert marks["1:5"].kind == "unknown" and marks["1:5"].box is None  # 위치를 모르면 요약에만 셈
    assert summary_counts(list(marks.values())) == {"warning": 3, "review": 0, "unknown": 1, "change": 0}


def test_selected_proposals_turn_into_diff_marks(tmp_path):
    marks = {
        mark.key: mark
        for mark in build_marks(_report(tmp_path / "d.pptx"), 1, {"font.size@1:2", "font.color@1:3"})
    }
    assert marks["1:2"].kind == "change" and (marks["1:2"].before, marks["1:2"].after) == ("11pt", "14pt")
    assert (marks["1:3"].before, marks["1:3"].after) == ("#C8C8C8", "#767676")
    assert marks["1:4"].kind == "warning"  # 선택하지 않은 개체는 그대로 문제 표시


@pytest.mark.parametrize(
    ("rule", "fid", "measured", "expected", "text"),
    [
        ("typography.hierarchy.inferred", "typography:8:hierarchy", 20.0, 30.0, ("20pt", "필요 30pt")),
        ("layout.overflow", "layout.overflow:7:3", 13.2, 0.0, ("13.2pt", "")),
        ("layout.density", "layout.density:2:lines", 19, 6, ("19줄", "최대 6줄")),
        ("layout.density", "layout.density:2:chars", 358, 300, ("358자", "최대 300자")),
        ("layout.margin", "layout.margin:1:4", 10.0, 36.0, ("10pt", "여백 36pt 이상")),
    ],
)
def test_metric_text_is_short_and_readable(rule, fid, measured, expected, text):
    assert metric_text(_finding(fid, rule, "1:2", "review", measured, expected)) == text


def test_slide_points_map_onto_the_visible_slide():
    rect = map_box(Box(480, 270, 96, 54), QRectF(100, 50, 960, 540), (960, 540))
    assert rect == QRectF(580, 320, 96, 54)
    half = map_box(Box(480, 270, 96, 54), QRectF(0, 0, 480, 270), (960, 540))
    assert half == QRectF(240, 135, 48, 27)


def test_badges_never_overlap_and_stay_inside():
    bounds = QRectF(0, 0, 800, 450)
    frames = [QRectF(50, 100 + index * 30, 300, 25) for index in range(6)] + [QRectF(760, 5, 60, 30)]
    sizes = [QSizeF(180, 26)] * len(frames)
    badges = place_badges(frames, sizes, bounds)
    for index, badge in enumerate(badges):
        assert bounds.contains(badge)
        for other in badges[index + 1 :]:
            assert not badge.intersects(other)


def test_painting_draws_frames_badges_and_summary(application, tmp_path):
    image = QImage(1000, 600, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    marks = build_marks(_report(tmp_path / "d.pptx"), 1, {"font.color@1:3"})
    paint_lens(painter, QRectF(20, 30, 960, 540), (960, 540), marks, QRectF(image.rect()))
    painter.end()
    colors = {image.pixelColor(x, y).name() for x in range(0, 1000, 4) for y in range(0, 600, 4)}
    assert len(colors) > 10  # 배지·테두리·색 견본이 그려짐


# ---------------------------------------------------------------- 컨트롤러 연결


class _Collection:
    def start(self):
        pass

    def stop(self, *, wait=True):
        pass


class _Tracker:
    def __init__(self, target):
        self.target = target

    def poll(self, _owned):
        return self.target


def _wait_for(condition, application, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def controller(application, tmp_path, monkeypatch):
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"pptx")
    settings = SimpleNamespace(
        load=lambda: WorkspaceSettings(tmp_path, False, "presentation"), save=lambda *a, **k: None
    )
    monkeypatch.setattr(overlay, "build_workspace_settings_repository", lambda: settings)
    monkeypatch.setattr(
        overlay, "build_report_repository", lambda: SimpleNamespace(find_successful=lambda *_: None)
    )
    screen = QRect(0, 0, 1600, 900)
    target = overlay.PowerPointWindow(
        7, QRect(0, 0, 1600, 900), "deck.pptx - PowerPoint", (0, 0, 1600, 900), screen
    )
    views = {"slide": 1}

    def reader(view_path, hwnd):
        assert hwnd == 7
        return SlideView(view_path, views["slide"], (300, 150, 1260, 690), (0, 0, 1600, 900), (960, 540))

    built = overlay.OverlayController(
        tracker=_Tracker(target),
        path_provider=lambda: path,
        collection_builder=lambda *_a, **_k: _Collection(),
        slide_view_reader=reader,
    )
    built.LENS_INTERVAL = 0.0
    built.poll_powerpoint()  # 열린 문서를 먼저 알아본 뒤 검사 결과를 띄운다
    built.overlay.show_report(_report(path))
    built.views = views
    yield built
    built.stop()
    built.settings_dialog.close()
    built.overlay.close()
    built.lens.close()


def _poll_until_lens(controller, application, expected_slide):
    def ready():
        controller.poll_powerpoint()
        view = controller._lens_view
        return controller.lens.isVisible() and view is not None and view.slide == expected_slide

    return _wait_for(ready, application)


def test_lens_follows_the_slide_shown_in_powerpoint(controller, application):
    assert _poll_until_lens(controller, application, 1)
    assert {mark.key for mark in controller.lens.marks} == {"1:2", "1:3", "1:4", "1:5"}
    assert controller.lens.geometry() == QRect(0, 0, 1600, 900)
    controller.views["slide"] = 2  # 사용자가 PowerPoint에서 다음 슬라이드로 넘김
    assert _poll_until_lens(controller, application, 2)
    assert {mark.key for mark in controller.lens.marks} == {"2:2"}


def test_selecting_a_proposal_shows_the_change_on_the_slide(controller, application):
    assert _poll_until_lens(controller, application, 1)
    controller.overlay.proposal_items["font.size@1:2"].setCheckState(overlay.Qt.CheckState.Checked)
    application.processEvents()
    marks = {mark.key: mark for mark in controller.lens.marks}
    assert marks["1:2"] == LensMark(
        marks["1:2"].key, marks["1:2"].box, "change", "글자 크기", "", before="11pt", after="14pt"
    )


def test_lens_hides_when_turned_off_stale_or_powerpoint_leaves(controller, application):
    assert _poll_until_lens(controller, application, 1)
    controller.overlay.lens_check.setChecked(False)
    assert not controller.lens.isVisible()
    controller.overlay.lens_check.setChecked(True)
    assert _poll_until_lens(controller, application, 1)
    controller.overlay.mark_report_stale("재검사 필요")
    controller.poll_powerpoint()
    assert not controller.lens.isVisible()
    controller.tracker.target = None
    controller.poll_powerpoint()
    assert not controller.lens.isVisible()


def test_unsaved_or_unreadable_view_hides_the_lens(controller, application):
    controller.slide_view_reader = lambda *_args: None  # 저장 안 한 편집, 슬라이드 쇼 등
    controller.poll_powerpoint()
    _wait_for(lambda: controller._lens_probe is None, application)
    controller.poll_powerpoint()
    assert not controller.lens.isVisible()
