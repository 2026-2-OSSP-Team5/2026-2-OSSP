from pathlib import Path

from slideguard.domain.models import DeckSnapshot

from .copies import saved_pptx_copy
from .session import PowerPointReadSession
from .snapshot import extract_snapshot


class PowerPointDeckReader:
    def __init__(self, session: PowerPointReadSession):
        self._session = session

    def read(self, path: Path) -> DeckSnapshot:
        with saved_pptx_copy(path) as saved:
            return self._session.read_copy(
                saved.copy_path, lambda presentation: extract_snapshot(presentation, saved)
            )
