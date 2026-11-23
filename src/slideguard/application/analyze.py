import logging
from pathlib import Path
from time import perf_counter

from slideguard.domain.models import AnalysisReport, DeckSnapshot
from slideguard.domain.policy import AnalysisPolicy

from .ports import CheckRule, DeckReader, ProposalPlanner, ReportRepository

LOGGER = logging.getLogger("slideguard.analyze")


class AnalyzePresentation:
    def __init__(
        self,
        reader: DeckReader,
        rules: tuple[CheckRule, ...] = (),
        planner: ProposalPlanner | None = None,
        repository: ReportRepository | None = None,
        finding_filter=None,
    ):
        """finding_filter(deck, findings) → (남길 판정, 부가 정보): 표지·출처 슬라이드 제외 등."""
        self.finding_filter = finding_filter
        self.reader = reader
        self.rules = rules
        self.planner = planner
        self.repository = repository

    def execute(
        self, path: Path, policy: AnalysisPolicy, *, incremental=False, on_progress=None
    ) -> AnalysisReport:
        started = perf_counter()
        read = getattr(self.reader, "read_incremental", None) if incremental else None
        deck = read(path, on_progress=on_progress) if read is not None else self.reader.read(path)
        read_seconds = perf_counter() - started
        report = self.analyze_snapshot(deck, policy)
        LOGGER.info(
            "ANALYZE_TIMING | file=%s | slides=%s shapes=%s | read=%.2fs check+plan=%.2fs total=%.2fs",
            Path(path).name,
            deck.slide_count,
            len(deck.shapes),
            read_seconds,
            perf_counter() - started - read_seconds,
            perf_counter() - started,
        )
        return report

    def analyze_snapshot(self, deck: DeckSnapshot, policy: AnalysisPolicy) -> AnalysisReport:
        report = AnalysisReport(deck=deck, policy_key=policy.cache_key)
        for rule in self.rules:
            report.findings.extend(rule.evaluate(report.deck, policy))
        if self.finding_filter is not None:
            report.findings, _info = self.finding_filter(report.deck, report.findings)
        report.executed_rules = tuple(rule.id for rule in self.rules)
        if self.rules:
            report.status = "analyzed"
            if self.planner is not None:
                report.proposals = self.planner.propose(report, policy)
        if self.repository is not None:
            self.repository.save(report)
        return report
