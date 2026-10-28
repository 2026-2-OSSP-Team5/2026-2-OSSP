from collections.abc import Callable
from pathlib import Path


class SavedFileMonitor:
    def __init__(self, path: Path, on_saved: Callable[[Path], None]):
        self.path = path
        self.on_saved = on_saved

    def start(self) -> None:
        raise NotImplementedError("Saved file monitoring is not available")

    def stop(self) -> None:
        raise NotImplementedError("Saved file monitoring is not available")
