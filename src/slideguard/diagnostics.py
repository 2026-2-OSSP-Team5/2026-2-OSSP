"""개발팀 확인용 진단 기록 (사용자 화면에는 아무것도 띄우지 않음).

desktop.main()이 만든 slideguard.log(같은 SlideGuard 폴더)에 아래 내용을 덧붙인다.
  - UNCAUGHT / UNCAUGHT_THREAD / UNRAISABLE : 처리되지 않은 예외와 호출 위치(traceback)
  - QT        : Qt 경고·오류 메시지
  - CONSOLE   : 콘솔 없는 exe에서 print()·경고로 나온 글 (그대로 두면 sys.stderr가 None이라 예외가 남)
  - PREVIOUS_RUN_NOT_CLOSED : 지난 실행이 정상 종료되지 않음 (강제 종료·충돌·전원 꺼짐)
  - EXIT      : 종료 시 처리되지 않은 예외 개수
그리고 같은 폴더에
  crash.log     파이썬이 잡지 못하는 비정상 종료(접근 위반 등) 때 스레드별 실행 위치 (faulthandler)
  running.json  실행 중 표시. 정상 종료하면 지워진다.

사용: 로그 파일 핸들러를 붙인 뒤 install_diagnostics("desktop", 폴더) 한 번 호출.
끄기: 환경 변수 SLIDEGUARD_LOG=off (테스트는 tests/conftest.py에서 꺼 둔다)
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import logging
import os
import platform
import sys
import threading
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger("slideguard.diagnostics")
ROOT = logging.getLogger("slideguard")
CRASH_LOG_LIMIT = 1_000_000  # crash.log가 이보다 커지면 crash.log.1로 넘기고 새로 시작

_installed: Path | None = None
_uncaught_count = 0  # Qt는 예외가 나도 계속 실행되므로 종료할 때 개수를 함께 남긴다
_crash_file = None  # faulthandler가 쓰는 파일은 프로그램이 끝날 때까지 열어 둬야 한다


def install_diagnostics(program: str, folder: Path) -> Path | None:
    """진단 기록을 켠다. 기록 폴더를 돌려주고, 실패하면 None (진단 때문에 프로그램이 멈추면 안 됨)."""
    global _installed
    if os.environ.get("SLIDEGUARD_LOG", "").lower() == "off":
        return None
    if _installed is not None:  # 두 번 불러도 한 번만 설치
        return _installed
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _ensure_file_output(folder / "slideguard.log")
        _replace_missing_console()
        _check_previous_run(folder / "running.json")
        _mark_running(folder / "running.json", program)
        _install_exception_hooks()
        _install_crash_dump(folder / "crash.log")
        _install_qt_messages()
        _installed = folder
        LOGGER.info(
            "DIAGNOSTICS_ON | program=%s python=%s os=%s exe=%s pid=%s",
            program,
            platform.python_version(),
            platform.platform(),
            bool(getattr(sys, "frozen", False)),
            os.getpid(),
        )
        return folder
    except Exception:  # noqa: BLE001 - 진단 설치 실패는 조용히 넘어간다
        return None


# ---------------------------------------------------------------- 설치 단계


def _ensure_file_output(path: Path) -> None:
    """desktop.main()이 파일 핸들러를 못 붙였을 때만 직접 붙인다 (기록이 사라지지 않게)."""
    if any(isinstance(handler, logging.FileHandler) for handler in ROOT.handlers):
        return
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    ROOT.addHandler(handler)
    if ROOT.level == logging.NOTSET or ROOT.level > logging.INFO:
        ROOT.setLevel(logging.INFO)


class _LogStream:
    """콘솔이 없는 exe에서 print()·경고 출력을 로그로 보내는 대체 출력."""

    def __init__(self, level: int):
        self.level = level

    def write(self, text: str) -> int:
        if text.strip():
            LOGGER.log(self.level, "CONSOLE | %s", text.rstrip())
        return len(text)

    def flush(self) -> None:
        pass


def _replace_missing_console() -> None:
    # 창 모드로 빌드한 exe는 sys.stdout / sys.stderr 가 None이라 print()만 해도 예외가 난다.
    if sys.stdout is None:
        sys.stdout = _LogStream(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logging.WARNING)


def _check_previous_run(marker: Path) -> None:
    if not marker.exists():
        return
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    LOGGER.warning(
        "PREVIOUS_RUN_NOT_CLOSED | program=%s pid=%s started=%s (강제 종료·충돌·전원 꺼짐 가능성, crash.log 확인)",
        previous.get("program"),
        previous.get("pid"),
        previous.get("started"),
    )


def _mark_running(marker: Path, program: str) -> None:
    started = datetime.now().isoformat(timespec="seconds")
    marker.write_text(
        json.dumps({"program": program, "pid": os.getpid(), "started": started}), encoding="utf-8"
    )

    def on_exit():
        if _uncaught_count:
            LOGGER.warning("EXIT | 처리되지 않은 예외 %s건 (위의 UNCAUGHT 기록 확인)", _uncaught_count)
        else:
            LOGGER.info("EXIT | 처리되지 않은 예외 없음")
        try:
            marker.unlink()
        except OSError:
            pass

    atexit.register(on_exit)


def _install_exception_hooks() -> None:
    previous_hook = sys.excepthook

    def log_uncaught(kind, value, trace):
        global _uncaught_count
        if not issubclass(kind, KeyboardInterrupt):
            _uncaught_count += 1
            LOGGER.critical("UNCAUGHT | %s: %s", kind.__name__, value, exc_info=(kind, value, trace))
        previous_hook(kind, value, trace)

    def log_thread(args):
        global _uncaught_count
        if args.exc_type is not SystemExit:
            _uncaught_count += 1
            name = args.thread.name if args.thread is not None else "?"
            LOGGER.critical(
                "UNCAUGHT_THREAD | thread=%s %s: %s",
                name,
                args.exc_type.__name__,
                args.exc_value,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

    def log_unraisable(args):
        LOGGER.error(
            "UNRAISABLE | %s: %s (object=%r)",
            type(args.exc_value).__name__,
            args.exc_value,
            args.object,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread
    sys.unraisablehook = log_unraisable


def _install_crash_dump(path: Path) -> None:
    # 참고: Windows에서는 COM 연결이 끊길 때(0x80010108 등) 프로그램이 계속 돌아가도 기록이 남을 수 있다.
    #       그 줄만 있고 slideguard.log에 PREVIOUS_RUN_NOT_CLOSED가 없으면 실제 충돌이 아니다.
    global _crash_file
    if path.exists() and path.stat().st_size > CRASH_LOG_LIMIT:
        path.replace(path.with_name(path.name + ".1"))
    _crash_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - 프로그램이 끝날 때까지 열어 둔다
    _crash_file.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} pid={os.getpid()} ===\n")
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)


def _install_qt_messages() -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handle(kind, context, message):
        LOGGER.log(levels.get(kind, logging.WARNING), "QT | %s (%s:%s)", message, context.file, context.line)

    qInstallMessageHandler(handle)
