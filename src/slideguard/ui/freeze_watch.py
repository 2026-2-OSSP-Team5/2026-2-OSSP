"""화면 멈춤 감시: 화면(UI) 스레드가 몇 초 이상 응답하지 않으면 그때 어디서 멈췄는지 로그에 남긴다.

사용자에게는 아무것도 띄우지 않는다. slideguard.log에 아래처럼 남는다.
  UI_FROZEN | 3.0초 이상 응답 없음      + 모든 스레드의 실행 위치 (화면 스레드가 맨 앞)
  UI_RECOVERED | 12.4초 동안 멈춘 뒤 다시 응답
"멈췄다"는 제보가 오면 이 두 줄 사이의 실행 위치를 보면 원인을 바로 찾을 수 있다.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from time import monotonic

from PySide6.QtCore import QObject, QTimer

LOGGER = logging.getLogger("slideguard.freeze")


class FreezeWatch(QObject):
    """화면 스레드의 QTimer가 심장 박동을 남기고, 감시 스레드가 박동이 끊겼는지 본다."""

    def __init__(self, limit: float = 3.0, beat: float = 0.25, clock=monotonic, parent=None):
        super().__init__(parent)
        self.limit = limit
        self.clock = clock
        self._last_beat = clock()
        self._frozen_since: float | None = None
        self._ui_thread = threading.get_ident()
        self._stopped = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(int(beat * 1000))
        self._timer.timeout.connect(self._beat)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._last_beat = self.clock()
        self._timer.start()
        self._thread = threading.Thread(target=self._watch, name="ui-freeze-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._timer.stop()

    def _beat(self) -> None:
        now = self.clock()
        if self._frozen_since is not None:
            LOGGER.warning("UI_RECOVERED | %.1f초 동안 멈춘 뒤 다시 응답", now - self._frozen_since)
            self._frozen_since = None
        self._last_beat = now

    def check(self) -> bool:
        """감시 한 번. 새로 멈춘 것을 발견해 기록했으면 True."""
        silent = self.clock() - self._last_beat
        if silent < self.limit or self._frozen_since is not None:
            return False
        self._frozen_since = self._last_beat
        LOGGER.warning("UI_FROZEN | %.1f초 이상 응답 없음\n%s", silent, stacks(self._ui_thread))
        return True

    def _watch(self) -> None:
        while not self._stopped.wait(0.5):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - 감시 때문에 프로그램이 멈추면 안 된다
                LOGGER.debug("FREEZE_WATCH_FAILED", exc_info=True)


def stacks(first: int | None = None) -> str:
    """모든 스레드의 실행 위치 (first 스레드를 맨 앞에)."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    order = sorted(frames, key=lambda ident: ident != first)
    parts = []
    for ident in order:
        label = "화면 스레드" if ident == first else names.get(ident, "?")
        parts.append(f"--- {label} ({ident})\n" + "".join(traceback.format_stack(frames[ident])))
    return "\n".join(parts)


def install_freeze_watch(app) -> FreezeWatch | None:
    """감시를 켠다. 실패해도 프로그램 시작을 막지 않는다."""
    try:
        watch = FreezeWatch(parent=app if isinstance(app, QObject) else None)
        watch.start()
        if isinstance(app, QObject):
            app.aboutToQuit.connect(watch.stop)
        else:
            app._slideguard_freeze_watch = watch  # 부모가 없으면 참조를 붙들어 둔다
        return watch
    except Exception:  # noqa: BLE001
        LOGGER.debug("FREEZE_WATCH_UNAVAILABLE", exc_info=True)
        return None
