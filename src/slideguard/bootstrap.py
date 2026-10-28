from slideguard.adapters.ooxml.reader import OoxmlDeckReader
from slideguard.application.analyze import AnalyzePresentation


def build_analyzer() -> AnalyzePresentation:
    """Build the analyzer with the configured reader and rules."""
    return AnalyzePresentation(reader=OoxmlDeckReader(), rules=())
