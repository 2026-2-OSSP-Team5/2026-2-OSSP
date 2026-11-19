"""슬라이드 렌즈(검사 결과를 슬라이드 위에 겹쳐 그리는 오버레이) 테스트. PowerPoint 없이 확인한다."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from slideguard.adapters.powerpoint.live_edit import LiveResult
from slideguard.adapters.storage.sqlite import WorkspaceSettings
from slideguard.domain.models import AnalysisReport, Box, ChangeProposal, DeckSnapshot, Finding
from slideguard.ui import overlay
from slideguard.ui.slide_lens import (
    BadgeHandle,
    InputActivity,
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
    assert not marks["1:2"].applied  # 패널에서 선택만 함 → 점선


def test_marks_offer_proposals_as_options_and_show_applied_ones(tmp_path):
    report = _report(tmp_path / "d.pptx")
    marks = {mark.key: mark for mark in build_marks(report, 1, applied_ids={"font.size@1:2"})}
    option = marks["1:2"].options[0]
    assert option.proposal_ids == ("font.size@1:2",) and option.checked
    assert option.label == "글자 크기 11pt → 14pt"
    assert marks["1:2"].kind == "change" and marks["1:2"].applied
    assert marks["1:2"].message == "메시지"
    assert not marks["1:3"].options[0].checked and marks["1:3"].kind == "warning"
    assert marks["1:4"].options == ()


def test_bundled_proposals_toggle_together(tmp_path):
    report = _report(tmp_path / "d.pptx")
    report.proposals += [
        ChangeProposal("shape.height@1:4#overflow:1:4", "x", "1:4", "shape.height", 60, 90, 0.1),
        ChangeProposal(
            "shape.position@1:6#overflow:1:4", "x", "1:6", "shape.position", (50, 170), (50, 190), 0.1
        ),
    ]
    marks = {mark.key: mark for mark in build_marks(report, 1)}
    (option,) = marks["1:4"].options
    assert option.proposal_ids == ("shape.height@1:4#overflow:1:4", "shape.position@1:6#overflow:1:4")


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
    views = {"slide": 1, "saved": True}

    def reader(view_path, hwnd):
        assert hwnd == 7
        return SlideView(
            view_path, views["slide"], (300, 150, 1260, 690), (0, 0, 1600, 900), (960, 540), views["saved"]
        )

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
    mark = {mark.key: mark for mark in controller.lens.marks}["1:2"]
    assert (mark.kind, mark.title, mark.before, mark.after, mark.applied) == (
        "change",
        "글자 크기",
        "11pt",
        "14pt",
        False,
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


def test_unreadable_view_hides_the_lens(controller, application):
    assert _poll_until_lens(controller, application, 1)
    controller.slide_view_reader = lambda *_args: None  # 슬라이드 쇼, 여러 슬라이드 보기 등
    controller._lens_force = True
    assert _wait_for(lambda: (controller.poll_powerpoint(), not controller.lens.isVisible())[1], application)


def test_user_edits_hide_the_lens_but_our_live_edits_keep_it(controller, application):
    assert _poll_until_lens(controller, application, 1)
    controller.views["saved"] = False  # 사용자가 직접 고치고 저장하지 않음
    controller._lens_force = True
    assert _wait_for(lambda: (controller.poll_powerpoint(), not controller.lens.isVisible())[1], application)
    controller._live_hash = controller.overlay._report.deck.source_sha256  # 렌즈 체크로 바꾼 상태
    controller._lens_force = True
    assert _wait_for(lambda: (controller.poll_powerpoint(), controller.lens.isVisible())[1], application)


# ---------------------------------------------------------------- 배지 펼치기·체크


def _click(widget, point):
    for kind in (QMouseEvent.Type.MouseMove, QMouseEvent.Type.MouseButtonPress):
        event = QMouseEvent(
            kind,
            QPointF(point),
            QPointF(point),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(widget, event)


def test_hovering_a_badge_expands_it_and_clicking_a_row_toggles(application, tmp_path):
    mark = {m.key: m for m in build_marks(_report(tmp_path / "d.pptx"), 1)}["1:2"]
    handle = BadgeHandle()
    handle.set_mark(mark, QRect(100, 100, 180, 26))
    assert handle.geometry() == QRect(100, 100, 180, 26) and not handle.expanded
    seen = []
    handle.toggled.connect(lambda ids, checked: seen.append((ids, checked)))
    handle.expand()  # 마우스를 올림
    assert handle.expanded and handle.width() >= 300 and handle.height() > 26
    row = handle._layout()["rows"][0]
    _click(handle, row.center())
    assert seen == [(("font.size@1:2",), True)]
    handle.set_busy(True)  # 적용 중에는 다시 누를 수 없음
    _click(handle, row.center())
    assert len(seen) == 1
    handle.set_busy(False)
    handle.collapse()
    assert handle.geometry() == QRect(100, 100, 180, 26)
    handle.close()


def test_controller_applies_and_reverts_from_the_lens(controller, application):
    calls = []

    class FakeEditor:
        applied_ids: set = set()

        def apply(self, report, ids):
            calls.append(("apply", tuple(ids)))
            self.applied_ids = set(ids)
            return LiveResult(applied=list(ids))

        def revert(self, ids):
            calls.append(("revert", tuple(ids)))
            self.applied_ids = set()
            return LiveResult(applied=list(ids))

        def forget(self):
            self.applied_ids = set()

    controller.live_editor = FakeEditor()
    assert _poll_until_lens(controller, application, 1)
    assert set(controller.lens.handles) == {"1:2", "1:3", "1:4"}  # 위치를 모르는 1:5는 요약에만
    controller.lens.handles["1:2"].toggled.emit(("font.size@1:2",), True)
    assert _wait_for(lambda: controller._live_task is None and calls, application)
    application.processEvents()
    mark = {m.key: m for m in controller.lens.marks}["1:2"]
    assert calls == [("apply", ("font.size@1:2",))] and mark.applied and mark.options[0].checked
    controller.lens.option_toggled.emit(("font.size@1:2",), False)
    assert _wait_for(lambda: controller._live_task is None and len(calls) == 2, application)
    application.processEvents()
    assert calls[1] == ("revert", ("font.size@1:2",))
    assert {m.key: m for m in controller.lens.marks}["1:2"].kind == "warning"


def test_failed_live_edit_shows_a_note_on_the_badge(controller, application):
    class Closed:
        applied_ids: set = set()

        def apply(self, report, ids):
            from slideguard.adapters.powerpoint.live_edit import LiveEditUnavailable

            raise LiveEditUnavailable("PowerPoint에서 이 문서가 열려 있을 때만 바로 고칠 수 있습니다.")

        def forget(self):
            pass

    controller.live_editor = Closed()
    assert _poll_until_lens(controller, application, 1)
    controller.lens.option_toggled.emit(("font.size@1:2",), True)
    assert _wait_for(lambda: controller.lens.handles["1:2"].note != "", application)
    assert "열려 있을 때만" in controller.lens.handles["1:2"].note
    assert not controller.lens.handles["1:2"].busy


# ---------------------------------------------------------------- 바쁜 커서 줄이기


def test_input_activity_asks_once_per_input_after_a_short_pause():
    state = {"info": (100, 0.05)}
    activity = InputActivity(lambda: state["info"], settle=0.3)
    assert not activity.due("lens")  # 아직 입력 중
    state["info"] = (100, 0.5)
    assert activity.due("lens")
    assert not activity.due(
        "lens"
    )  # 같은 입력으로는 다시 묻지 않음 (가만히 있으면 PowerPoint를 건드리지 않음)
    assert activity.due("path")  # 이름별로 따로
    state["info"] = (250, 0.4)
    assert activity.due("lens")
    activity.reset("lens")
    assert activity.due("lens")
    assert InputActivity(lambda: None).due("x")  # Windows가 아니면 제한 없음


def test_idle_user_means_no_more_powerpoint_queries(controller, application):
    probes = []
    reader = controller.slide_view_reader

    def counting(path, hwnd):
        probes.append(path)
        return reader(path, hwnd)

    controller.slide_view_reader = counting
    controller.activity = InputActivity(lambda: (7, 10.0))
    controller._lens_force = True
    assert _wait_for(lambda: (controller.poll_powerpoint(), len(probes) == 1)[1], application)
    for _ in range(20):  # 사용자가 아무것도 하지 않음
        _wait_for(lambda: controller._lens_probe is None, application)
        controller.poll_powerpoint()
    assert len(probes) == 2  # 처음 강제로 한 번 + 아직 보지 못한 입력 한 번, 그 뒤로는 묻지 않음
