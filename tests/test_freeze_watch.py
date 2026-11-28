"""화면 멈춤 감시 테스트: 박동이 끊기면 한 번만 기록하고, 다시 뛰면 회복을 기록한다."""

import logging
import threading

from slideguard.ui.freeze_watch import FreezeWatch, stacks


def test_freeze_is_logged_once_with_stacks_then_recovery(caplog):
    clock = {"now": 100.0}
    watch = FreezeWatch(limit=3.0, clock=lambda: clock["now"])
    caplog.set_level(logging.WARNING, logger="slideguard.freeze")
    clock["now"] = 102.0
    assert not watch.check()  # 아직 3초가 안 됨
    clock["now"] = 104.0
    assert watch.check()
    assert not watch.check()  # 같은 멈춤은 한 번만
    assert "UI_FROZEN" in caplog.text and "화면 스레드" in caplog.text
    clock["now"] = 110.0
    watch._beat()
    assert "UI_RECOVERED | 10.0초" in caplog.text


def test_stacks_lists_every_thread_with_the_ui_thread_first():
    text = stacks(threading.get_ident())
    assert text.startswith("--- 화면 스레드") and "test_stacks_lists_every_thread" in text
