"""Saved Office document through the desktop, overlay, patcher, watcher, and history."""

import time
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from slideguard.bootstrap import build_analysis_policy, build_report_repository
from slideguard.ui import desktop, overlay

pytestmark = pytest.mark.powerpoint


def _add_text(shapes, text, y, size):
    box = shapes.add_textbox(Inches(1.5), Inches(y), Inches(5), Inches(0.6))
    _set_run(box, text, size)
    return box.shape_id


def _set_run(shape, text, size):
    run = shape.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0, 0, 0)


def _sizes(path):
    slides = Presentation(path).slides
    return [shape.text_frame.paragraphs[0].runs[0].font.size.pt for shape in slides[1].shapes]


def test_desktop_settings_save_locate_apply_recheck_and_history(
    powerpoint_environment, tmp_path, monkeypatch
):
    import pythoncom
    import win32com.client
    import win32gui

    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    source = nested / f"slideguard-{tmp_path.parent.name}.pptx"
    first_copy = tmp_path / "one.pptx"
    all_copy = tmp_path / "all.pptx"
    deck = Presentation()
    title_slide = deck.slides.add_slide(deck.slide_layouts[1])
    _set_run(title_slide.shapes.title, "Same size heading", 20)
    _set_run(title_slide.placeholders[1], "Same size body", 20)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    small_id = _add_text(slide.shapes, "Small first text", 2.0, 10)
    locked_id = _add_text(slide.shapes, "Small locked text", 3.0, 11)
    ignored = deck.slides.add_slide(deck.slide_layouts[6])
    _add_text(ignored.shapes, "Out of scope", 2.0, 9)
    deck.save(source)
    original = source.read_bytes()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, msg: errors.append((title, msg)))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda _self: QMessageBox.StandardButton.Ok)
    copy_paths = iter((first_copy, all_copy))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(next(copy_paths)), ""))

    app = QApplication.instance() or QApplication([])
    if app.platformName() != "windows":
        pytest.skip("The overlay coordinate check requires the Windows Qt platform")
    window = desktop.MainWindow()
    controller = overlay.OverlayController()
    window.connect_overlay(controller)
    widget = controller.overlay

    def wait_for(condition, label, seconds=35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pythoncom.PumpWaitingMessages()
            app.processEvents()
            if condition():
                return
            assert not errors, errors
            time.sleep(0.03)
        pytest.fail(f"{label}: {widget.status_label.text()} / {errors}")

    def active_document():
        return win32com.client.GetActiveObject("PowerPoint.Application").ActivePresentation

    def report_for_saved_source():
        report = widget._report
        if report is None:
            return False
        try:
            return report.deck.source_sha256 == overlay._sha256(source)
        except OSError:
            return False  # Office can temporarily deny reads while it replaces the saved file.

    def report_for(path):
        key = build_analysis_policy(source).cache_key
        return build_report_repository().find_successful(path, key)

    pythoncom.CoInitialize()
    office = document = None
    try:
        office = win32com.client.DispatchEx("PowerPoint.Application")
        document = office.Presentations.Open(str(source), ReadOnly=0, Untitled=0, WithWindow=-1)
        document.Windows.Item(1).Activate()
        assert overlay.active_presentation_path() == source
        frames = []

        def find_frame(hwnd, _):
            if win32gui.GetClassName(hwnd) == "PPTFrameClass" and source.stem in win32gui.GetWindowText(hwnd):
                frames.append(hwnd)
            return True

        win32gui.EnumWindows(find_frame, None)
        assert frames, "PowerPoint document did not create a native frame"
        with monkeypatch.context() as focus:
            focus.setattr(win32gui, "GetForegroundWindow", lambda: frames[0])
            controller.poll_powerpoint()
        controller.timer.stop()
        assert widget.isVisible() and widget.collapsed
        assert widget.geometry().top() > widget._target.rect.top()
        initial_position = widget.pos()
        shield = widget.shield_button
        center = shield.rect().center()
        QTest.mousePress(shield, Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(shield, center + QPoint(-40, 35))
        QTest.mouseRelease(shield, Qt.MouseButton.LeftButton, pos=center)
        assert widget.collapsed and widget.pos() != initial_position
        assert widget._target.rect.contains(widget.geometry())

        window.settings_button.click()
        settings = controller.settings_dialog
        settings.folder_edit.setText(str(workspace))
        settings.recursive.setChecked(True)
        settings.strength.setCurrentIndex(settings.strength.findData("strong"))
        settings.slides_edit.setText("1-2")
        settings.property_locks["font_color"].setChecked(True)
        settings.shape_locks_edit.setText(f"2:{locked_id}")
        settings.threshold_fields["minimum_font"].setText("15")
        settings.threshold_fields["recommended_font"].setText("15")
        settings.save()
        policy = build_analysis_policy(source)
        assert policy.slides == (1, 2) and policy.strength == "strong"
        assert policy.locked_properties == {"font_color"} and policy.locked_shapes == {f"2:{locked_id}"}
        assert not build_analysis_policy(first_copy).locked_shapes
        wait_for(report_for_saved_source, "initial analysis of previously unchecked nested file")
        baseline = widget._report
        assert widget.count_badge.text() == str(len(baseline.proposals)) == "2"
        assert widget.count_badge.isVisible()
        assert "검사 완료" in window.file_table.item(0, 0).text()
        window.file_table.setCurrentCell(0, 0)
        assert window.inspection_stack.currentWidget() is window.results_page
        assert window.report_tabs.tabText(1) == "수정안 (2건)"
        assert window.proposal_table.rowCount() == 2
        assert any(f.id.endswith(":hierarchy") for f in baseline.findings)
        assert any(f.shape_key == f"2:{locked_id}" for f in baseline.findings)
        assert all(not f.shape_key.startswith("3:") for f in baseline.findings)
        assert all(p.shape_key != f"2:{locked_id}" for p in baseline.proposals)
        proposal = next(p for p in baseline.proposals if p.shape_key == f"2:{small_id}")
        assert proposal.after == 15

        widget.shield_button.click()
        row = next(i for i, f in enumerate(baseline.findings) if f.id.endswith(":hierarchy"))
        widget.findings_table.setCurrentCell(row, 0)
        active_document().Windows.Item(1).View.GotoSlide(2)
        widget.locate_button.click()
        assert active_document().Windows.Item(1).View.Slide.SlideIndex == 1
        assert controller.outline.isVisible() and widget.collapsed, widget.location_label.text()
        assert widget._target.rect.contains(controller.outline.geometry().center())

        widget.shield_button.click()
        widget.proposals_list.setCurrentItem(widget.proposal_items[proposal.id])
        widget.apply_one_button.click()
        wait_for(
            lambda: (
                controller._last_copy_outcome is not None
                and controller._last_copy_outcome.output_path == first_copy
                and controller._apply_thread is None
            ),
            "single proposal copy with reinspection",
        )
        one = controller._last_copy_outcome
        assert one.applied_ids == (proposal.id,)
        assert _sizes(first_copy) == [15, 11] and source.read_bytes() == original
        assert one.reinspection_error is None and one.reinspection.comparison.improved >= 1

        widget.apply_all_button.click()
        wait_for(
            lambda: (
                controller._last_copy_outcome is not None
                and controller._last_copy_outcome.output_path == all_copy
                and controller._apply_thread is None
            ),
            "all proposals copy with reinspection",
        )
        all_result = controller._last_copy_outcome
        assert len(all_result.applied_ids) == 2
        assert all_result.reinspection_error is None
        assert all_result.reinspection.comparison.improved >= 2
        assert _sizes(all_copy) == [15, 11] and source.read_bytes() == original
        assert report_for(first_copy) and report_for(all_copy)

        widget.apply_original_button.click()
        wait_for(
            lambda: (
                controller._apply_thread is None
                and report_for_saved_source()
                and widget._report.deck.source_sha256 != baseline.deck.source_sha256
            ),
            "original apply, backup, and automatic reinspection",
        )
        assert _sizes(source) == [15, 11]
        backups = tuple((tmp_path / "state" / "SlideGuard" / "backups").glob("*.pptx"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert widget._report.comparison.improved >= 2

        current = active_document()
        text = current.Slides.Item(2).Shapes.Item(1).TextFrame.TextRange
        text.Font.Size = 9
        current.Save()
        text = current = None
        wait_for(report_for_saved_source, "PowerPoint save after application")
        assert any(p.shape_key == f"2:{small_id}" for p in widget._report.proposals)
        assert widget._report.comparison.new_or_worse >= 1

        window.history_tab_button.click()
        window.history_table.selectRow(0)
        assert window.current_report.deck.source_sha256 == overlay._sha256(source)
        assert "재검사 비교" in window.result_label.text()
        saved_count = window.history_table.rowCount()
        window.show_file_tab()
        assert window.current_report.deck.source_sha256 == overlay._sha256(source)
        assert window.inspection_stack.currentWidget() is window.results_page
        controller.restart_collection()
        wait_for(lambda: controller.collection.collector.wait_idle(0), "startup hash check")
        assert len(build_report_repository().list_recent()) == saved_count

        controller.collection.stop()
        current = active_document()
        current.Slides.Item(2).Shapes.Item(1).TextFrame.TextRange.Font.Size = 18
        current.Save()
        current = None
        window.refresh_files()
        assert "변경됨" in window.file_table.item(0, 0).text()
        controller.restart_collection()
        wait_for(report_for_saved_source, "dirty revision saved while the watcher was stopped")
        assert "검사 완료" in window.file_table.item(0, 0).text()
        assert len(build_report_repository().list_recent()) == saved_count + 1

        controller._set_active_path(first_copy)
        assert widget._report is None
        assert not widget.count_badge.isVisible() and not widget.apply_all_button.isEnabled()
        assert not errors
    finally:
        controller.stop()
        controller.settings_dialog.close()
        controller.overlay.close()
        window.closing.disconnect(controller.request_exit)
        window.close()
        try:
            if document is not None:
                try:
                    connected = win32com.client.GetActiveObject("PowerPoint.Application")
                except pythoncom.com_error:
                    connected = None
                if connected is not None:
                    for index in range(connected.Presentations.Count, 0, -1):
                        candidate = connected.Presentations.Item(index)
                        if Path(str(candidate.FullName)).resolve() == source:
                            if not candidate.Saved:
                                candidate.Save()
                            candidate.Close()
                            break
                    candidate = connected = None
        finally:
            document = office = None
            pythoncom.CoUninitialize()
