"""Purpose-based size recommendations; point values are not WCAG requirements."""

from slideguard.domain.models import DeckSnapshot, Finding
from slideguard.domain.policy import AnalysisPolicy


class TypographyRule:
    id = "typography"

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]:
        raise NotImplementedError("Typography checks are not available")
