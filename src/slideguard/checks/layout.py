"""Overlap requires review and does not prove unintended placement."""

from slideguard.domain.models import DeckSnapshot, Finding
from slideguard.domain.policy import AnalysisPolicy


class LayoutRule:
    id = "layout"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        raise NotImplementedError("Layout checks are not available")
