"""진단 기록(slideguard.diagnostics) 테스트.

예외 훅·faulthandler는 프로세스 전체에 걸리므로, 각 경우를 별도 파이썬 프로세스로 실행해 확인한다.
desktop.main()처럼 slideguard 로거에 파일 핸들러를 먼저 붙이고 install_diagnostics를 부른다.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _run(code: str, folder: Path, *, with_handler: bool = True) -> subprocess.CompletedProcess:
    handler = (
        f"handler = logging.FileHandler({str(folder / 'slideguard.log')!r}, encoding='utf-8')\n"
        "logging.getLogger('slideguard').addHandler(handler)\n"
        "logging.getLogger('slideguard').setLevel(logging.INFO)\n"
        if with_handler
        else ""
    )
    script = (
        f"import sys, logging, pathlib\nsys.path.insert(0, {str(SRC)!r})\n"
        f"pathlib.Path({str(folder)!r}).mkdir(parents=True, exist_ok=True)\n"
        + handler
        + "from slideguard.diagnostics import install_diagnostics\n"
        + f"install_diagnostics('test', {str(folder)!r})\n"
        + textwrap.dedent(code)
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    env.pop("SLIDEGUARD_LOG", None)  # conftest가 꺼 둔 기록을 이 프로세스에서는 켠다
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)


def _log(folder: Path) -> str:
    return (folder / "slideguard.log").read_text(encoding="utf-8")


def test_clean_run_removes_running_marker(tmp_path):
    result = _run("logging.getLogger('slideguard.collect').warning('SAMPLE | hello')", tmp_path)
    assert result.returncode == 0, result.stderr
    log = _log(tmp_path)
    assert "DIAGNOSTICS_ON | program=test" in log
    assert "SAMPLE | hello" in log  # 다른 모듈 기록과 같은 파일
    assert "EXIT | 처리되지 않은 예외 없음" in log
    assert not (tmp_path / "running.json").exists()


def test_uncaught_exception_is_logged_with_traceback(tmp_path):
    result = _run("def broken():\n    raise ValueError('boom')\nbroken()", tmp_path)
    assert result.returncode != 0
    log = _log(tmp_path)
    assert "UNCAUGHT | ValueError: boom" in log
    assert "in broken" in log
    assert "처리되지 않은 예외 1건" in log


def test_thread_exception_is_logged(tmp_path):
    code = """
    import threading
    def work():
        raise RuntimeError('worker died')
    thread = threading.Thread(target=work, name='collector')
    thread.start(); thread.join()
    """
    assert _run(code, tmp_path).returncode == 0
    assert "UNCAUGHT_THREAD | thread=collector RuntimeError: worker died" in _log(tmp_path)


def test_hard_kill_is_reported_on_next_start(tmp_path):
    assert _run("import os; os._exit(3)", tmp_path).returncode == 3  # atexit 없이 강제 종료
    assert (tmp_path / "running.json").exists()
    _run("pass", tmp_path)
    assert "PREVIOUS_RUN_NOT_CLOSED | program=test" in _log(tmp_path)


def test_native_crash_is_written_to_crash_log(tmp_path):
    assert _run("import faulthandler; faulthandler._sigsegv()", tmp_path).returncode != 0
    crash = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error" in crash or "Windows fatal exception" in crash
    _run("pass", tmp_path)
    assert "PREVIOUS_RUN_NOT_CLOSED" in _log(tmp_path)


def test_windowed_exe_without_console_does_not_break_print(tmp_path):
    code = """
    sys.stdout = None
    sys.stderr = None
    from slideguard import diagnostics
    diagnostics._replace_missing_console()
    print('hello from print')
    print('warning text', file=sys.stderr)
    """
    result = _run(code, tmp_path)
    assert result.returncode == 0, result.stderr
    log = _log(tmp_path)
    assert "CONSOLE | hello from print" in log and "CONSOLE | warning text" in log


def test_qt_warnings_go_to_log(tmp_path):
    result = _run("from PySide6.QtCore import qWarning\nqWarning('qt says careful')", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "QT | qt says careful" in _log(tmp_path)


def test_adds_file_output_when_desktop_log_is_missing(tmp_path):
    result = _run("raise RuntimeError('no handler yet')", tmp_path, with_handler=False)
    assert result.returncode != 0
    assert "UNCAUGHT | RuntimeError: no handler yet" in _log(tmp_path)


def test_can_be_turned_off(tmp_path):
    script = (
        f"import sys; sys.path.insert(0, {str(SRC)!r})\n"
        "from slideguard.diagnostics import install_diagnostics\n"
        f"print(install_diagnostics('test', {str(tmp_path)!r}))\n"
    )
    env = dict(os.environ, SLIDEGUARD_LOG="off")
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.stdout.strip() == "None"
    assert not (tmp_path / "running.json").exists()


def test_unwritable_folder_does_not_stop_the_program(tmp_path):
    blocked = tmp_path / "not_a_folder"
    blocked.write_text("x")  # 폴더 자리에 파일이 있어 기록 폴더를 만들 수 없음
    script = (
        f"import sys; sys.path.insert(0, {str(SRC)!r})\n"
        "from slideguard.diagnostics import install_diagnostics\n"
        f"print(install_diagnostics('test', {str(blocked / 'logs')!r}))\n"
    )
    env = dict(os.environ)
    env.pop("SLIDEGUARD_LOG", None)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"
