from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from slideguard.ui import desktop, overlay, resident


@pytest.mark.parametrize("background", [False, True])
def test_desktop_startup_respects_background_mode(monkeypatch, tmp_path, background):
    events = []
    callbacks = []
    controller = object()

    class FakeWindow:
        def _raise_window(self):
            events.append("desktop.open")

        def show(self):
            events.append("desktop.show")

        def raise_(self):
            events.append("desktop.raise")

        def activateWindow(self):
            events.append("desktop.activate")

        def connect_overlay(self, value):
            assert value is controller
            events.append("desktop.connect_overlay")

    class FakeApplication:
        @staticmethod
        def instance():
            return application

    def run_event_loop():
        assert events == ([] if background else ["desktop.show", "desktop.raise", "desktop.activate"])
        assert len(callbacks) == 1
        callbacks.pop()()
        return 17

    application = SimpleNamespace(exec=run_event_loop)
    monkeypatch.setattr(desktop, "QApplication", FakeApplication)
    monkeypatch.setattr(desktop, "MainWindow", FakeWindow)
    monkeypatch.setattr(desktop, "report_database_path", lambda: tmp_path / "reports.sqlite3")
    monkeypatch.setattr(desktop, "_acquire_instance_lock", lambda _path: object())
    monkeypatch.setattr(resident, "listen_for_activation", lambda *_args: object())
    monkeypatch.setattr(resident, "create_tray", lambda *_args: object())
    monkeypatch.setattr(desktop.QTimer, "singleShot", lambda _delay, callback: callbacks.append(callback))
    monkeypatch.setattr(
        overlay,
        "start_overlay",
        lambda app: events.append("overlay.start") or controller,
    )

    assert desktop.main(background=background) == 17
    expected = [] if background else ["desktop.show", "desktop.raise", "desktop.activate"]
    assert events == expected + [
        "overlay.start",
        "desktop.connect_overlay",
    ]
    assert application._slideguard_main_window.keep_running_on_close
    log = (tmp_path / "slideguard.log").read_text(encoding="utf-8")
    assert "DESKTOP_STARTED" in log
    assert "DESKTOP_STOPPED" in log


@pytest.mark.parametrize("background", [False, True])
def test_second_launch_activates_resident_only_for_manual_launch(monkeypatch, tmp_path, background):
    activated = []
    application = object()
    monkeypatch.setattr(desktop, "QApplication", SimpleNamespace(instance=lambda: application))
    monkeypatch.setattr(desktop, "report_database_path", lambda: tmp_path / "reports.sqlite3")
    monkeypatch.setattr(desktop, "_acquire_instance_lock", lambda _path: None)
    monkeypatch.setattr(resident, "activate_running_instance", activated.append)

    assert desktop.main(background=background) == 0
    assert activated == ([] if background else [tmp_path / "reports.sqlite3"])


def test_local_activation_opens_existing_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls = []
    database = tmp_path / "activation.sqlite3"
    server = resident.listen_for_activation(app, database, lambda: calls.append("opened"))
    try:
        assert server.isListening(), server.errorString()
        assert resident.activate_running_instance(database)
        QCoreApplication.processEvents()
        assert calls == ["opened"]
    finally:
        server.close()
        server.deleteLater()


def test_management_window_can_close_while_resident_continues(monkeypatch):
    from PySide6.QtGui import QCloseEvent

    window = SimpleNamespace(worker=None, keep_running_on_close=True, hide=lambda: hidden.append(True))
    hidden = []
    event = QCloseEvent()
    desktop.MainWindow.closeEvent(window, event)
    assert hidden == [True]
    assert not event.isAccepted()
