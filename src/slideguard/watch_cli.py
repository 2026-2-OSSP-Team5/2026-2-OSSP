import logging
import sys
import time
from pathlib import Path

from .adapters.watching.monitor import LOGGER, SavedFileMonitor


def watch_directory(directory: Path, *, recursive: bool, settle_seconds: float, log_file: Path | None) -> int:
    """Watch a directory in the foreground and log saved PPTX changes."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    monitor = None
    old_level, old_propagate = LOGGER.level, LOGGER.propagate
    try:
        if log_file is not None:
            if log_file.suffix.lower() != ".log":
                raise ValueError("--log-file must have a .log extension")
            log_file = log_file.resolve()
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        for handler in handlers:
            handler.setFormatter(formatter)
            LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
        monitor = SavedFileMonitor(directory, recursive=recursive, settle_seconds=settle_seconds)
        monitor.start()
        LOGGER.info("Ctrl+C로 감시를 종료합니다.")
        if log_file is not None:
            LOGGER.info("LOG_FILE | file=%s", log_file)
        try:
            while monitor.is_running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            return 0
        LOGGER.error("WATCH_ERROR | monitoring thread stopped unexpectedly")
        return 1
    finally:
        if monitor is not None:
            monitor.stop()
        for handler in handlers:
            LOGGER.removeHandler(handler)
            handler.close()
        LOGGER.setLevel(old_level)
        LOGGER.propagate = old_propagate
