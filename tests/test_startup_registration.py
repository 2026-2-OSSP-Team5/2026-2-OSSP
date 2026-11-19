import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from slideguard.adapters import startup


class FakeRegistry:
    HKEY_CURRENT_USER = "current-user"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def OpenKey(self, root, path, *_args):
        assert root == self.HKEY_CURRENT_USER
        assert path == startup.RUN_KEY
        return self

    CreateKeyEx = OpenKey

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, _key, name, _reserved, kind, value):
        self.values[name] = value, kind

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_registration_uses_current_user_and_removes_only_slideguard(monkeypatch):
    registry = FakeRegistry()
    registry.values["OtherApp"] = ("other.exe", registry.REG_SZ)
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(startup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(startup, "startup_command", lambda: '"C:\\Apps\\SlideGuard.exe" --background')

    assert not startup.is_enabled()
    startup.set_enabled(True)
    assert startup.is_enabled()
    assert registry.values[startup.VALUE_NAME][0].endswith(" --background")
    startup.set_enabled(False)
    startup.set_enabled(False)
    assert not startup.is_enabled()
    assert registry.values == {"OtherApp": ("other.exe", registry.REG_SZ)}


def test_packaged_command_quotes_executable_path(monkeypatch, tmp_path):
    executable = tmp_path / "My App" / "SlideGuard.exe"
    monkeypatch.setattr(startup, "sys", SimpleNamespace(executable=str(executable), frozen=True))
    assert startup.startup_command() == f'"{executable.resolve()}" --background'


def test_development_command_uses_absolute_script_and_pythonw(monkeypatch, tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(startup, "sys", SimpleNamespace(executable=str(tmp_path / "python.exe")))
    command = startup.startup_command()
    entry = Path(startup.__file__).resolve().parents[1] / "ui" / "desktop.py"
    assert str(pythonw) in command
    assert str(entry) in command
    assert command.endswith(" --background")


def test_missing_windowless_python_does_not_register_console_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "sys", SimpleNamespace(executable=str(tmp_path / "python.exe")))
    with pytest.raises(OSError, match="pythonw"):
        startup.startup_command()
