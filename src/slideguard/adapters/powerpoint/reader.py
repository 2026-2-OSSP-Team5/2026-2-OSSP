from pathlib import Path

from slideguard.adapters.ooxml.details import enrich_snapshot
from slideguard.domain.models import DeckSnapshot

from .copies import saved_pptx_copy
from .render import render_evidence
from .session import PowerPointReadSession
from .snapshot import extract_snapshot


class PowerPointDeckReader:
    def __init__(self, session: PowerPointReadSession, *, render: bool = False):
        self._session = session
        self._render = render

    def read(self, path: Path) -> DeckSnapshot:
        with saved_pptx_copy(path) as saved:

            def extract(presentation):
                snapshot = extract_snapshot(presentation, saved)
                if self._render:
                    snapshot = render_evidence(presentation, snapshot, saved.copy_path.with_suffix(".png"))
                return enrich_snapshot(snapshot, saved.copy_path)

            if self._render:
                return self._session.read_copy(saved.copy_path, extract, discard_probe_edits=True)
            return self._session.read_copy(saved.copy_path, extract)
