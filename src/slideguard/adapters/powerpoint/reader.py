from pathlib import Path

from slideguard.domain.models import DeckSnapshot


class PowerPointDeckReader:
    def read(self, path: Path) -> DeckSnapshot:
        raise NotImplementedError("PowerPoint COM reading is not available")
