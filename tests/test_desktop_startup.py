from types import SimpleNamespace

from slideguard.ui import desktop, overlay


def test_desktop_window_is_shown_before_overlay_starts(monkeypatch, tmp_path):
    events = []
    callbacks = []
    controller = object()

    class FakeWindow:
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
        assert events == ["desktop.show", "desktop.raise", "desktop.activate"]
        assert len(callbacks) == 1
        callbacks.pop()()
        return 17

    application = SimpleNamespace(exec=run_event_loop)
    monkeypatch.setattr(desktop, "QApplication", FakeApplication)
    monkeypatch.setattr(desktop, "MainWindow", FakeWindow)
    monkeypatch.setattr(desktop, "report_database_path", lambda: tmp_path / "reports.sqlite3")
    monkeypatch.setattr(desktop, "_acquire_instance_lock", lambda _path: object())
    monkeypatch.setattr(desktop.QTimer, "singleShot", lambda _delay, callback: callbacks.append(callback))
    monkeypatch.setattr(
        overlay,
        "start_overlay",
        lambda app: events.append("overlay.start") or controller,
    )

    assert desktop.main() == 17
    assert events == [
        "desktop.show",
        "desktop.raise",
        "desktop.activate",
        "overlay.start",
        "desktop.connect_overlay",
    ]
