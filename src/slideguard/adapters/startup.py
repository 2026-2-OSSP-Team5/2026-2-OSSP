"""Current-user Windows logon registration for the background desktop app."""

import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SlideGuard"


def startup_command() -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        arguments = [str(executable), "--background"]
    else:
        pythonw = executable.with_name("pythonw.exe")
        if not pythonw.is_file():
            raise OSError("창 없이 실행할 pythonw.exe를 찾을 수 없습니다.")
        entry = Path(__file__).resolve().parents[1] / "ui" / "desktop.py"
        arguments = [str(pythonw), str(entry), "--background"]
    command = subprocess.list2cmdline(arguments)
    if len(command) > 260:
        raise ValueError("자동 실행 경로가 너무 깁니다. SlideGuard를 더 짧은 경로에 설치하세요.")
    return command


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, kind = winreg.QueryValueEx(key, VALUE_NAME)
            return kind == winreg.REG_SZ and bool(command)
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool) -> None:
    if sys.platform != "win32":
        raise OSError("Windows에서 자동 실행을 설정할 수 있습니다.")
    import winreg

    if enabled:
        command = startup_command()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
