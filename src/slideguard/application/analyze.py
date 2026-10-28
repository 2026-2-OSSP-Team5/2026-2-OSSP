from pathlib import Path

from slideguard.domain.models import AnalysisReport
from slideguard.domain.policy import AnalysisPolicy

from .ports import CheckRule, DeckReader, ProposalPlanner, ReportRepository


class AnalyzePresentation:
    def __init__(
        self,
        reader: DeckReader,
        rules: tuple[CheckRule, ...] = (),
        planner: ProposalPlanner | None = None,
        repository: ReportRepository | None = None,
    ):
        self.reader = reader
        self.rules = rules
        self.planner = planner
        self.repository = repository

    def execute(self, path: Path, policy: AnalysisPolicy) -> AnalysisReport:
        report = AnalysisReport(deck=self.reader.read(path))
        for rule in self.rules:
            report.findings.extend(rule.evaluate(report.deck, policy))
        report.executed_rules = tuple(rule.id for rule in self.rules)
        if self.rules:
            report.status = "analyzed"
            if self.planner is not None:
                report.proposals = self.planner.propose(report, policy)
        if self.repository is not None:
            self.repository.save(report)
        return report
