from pathlib import Path
from typing import Protocol

from slideguard.domain.models import AnalysisReport, ChangeProposal, DeckSnapshot, Finding
from slideguard.domain.policy import AnalysisPolicy


class DeckReader(Protocol):
    def read(self, path: Path) -> DeckSnapshot: ...


class CheckRule(Protocol):
    id: str

    def evaluate(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> list[Finding]: ...


class ProposalPlanner(Protocol):
    def propose(self, report: AnalysisReport, policy: AnalysisPolicy) -> list[ChangeProposal]: ...


class ReportRepository(Protocol):
    def save(self, report: AnalysisReport) -> None: ...


class PresentationPatcher(Protocol):
    def apply_to_copy(
        self,
        report: AnalysisReport,
        proposal_ids: list[str],
        output: Path,
        policy: AnalysisPolicy,
    ) -> Path: ...
