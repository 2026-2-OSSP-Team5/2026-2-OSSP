from pathlib import Path

from slideguard.domain.models import AnalysisReport
from slideguard.domain.policy import AnalysisPolicy


class PowerPointPatcher:
    def apply_to_copy(
        self,
        report: AnalysisReport,
        proposal_ids: list[str],
        output: Path,
        policy: AnalysisPolicy,
    ) -> Path:
        raise NotImplementedError("PowerPoint copy modification is not available")
