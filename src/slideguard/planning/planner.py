from slideguard.domain.models import AnalysisReport, ChangeProposal
from slideguard.domain.policy import AnalysisPolicy


class MinimumChangePlanner:
    def propose(self, report: AnalysisReport, policy: AnalysisPolicy) -> list[ChangeProposal]:
        raise NotImplementedError("Change proposal generation is not available")
