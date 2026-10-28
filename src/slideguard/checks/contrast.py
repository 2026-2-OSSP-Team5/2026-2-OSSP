"""Use known foreground/background colors; unresolved colors must remain unknown."""

from slideguard.domain.models import DeckSnapshot, Finding
from slideguard.domain.policy import AnalysisPolicy


class ContrastRule:
    id = "contrast"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        raise NotImplementedError("Contrast checks are not available")
