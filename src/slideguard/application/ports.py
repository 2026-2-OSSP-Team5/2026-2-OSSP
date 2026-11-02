from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from slideguard.domain.models import AnalysisReport, ChangeProposal, DeckSnapshot, Finding
from slideguard.domain.policy import AnalysisPolicy


@dataclass(frozen=True)
class SavedRevision:
    """A validated saved version, not a completed extraction or analysis."""

    source_path: Path
    source_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class FileRemoved:
    """An observed path remained absent through the removal settling interval."""

    source_path: Path


type FileWatchEvent = SavedRevision | FileRemoved
type FileWatchCallback = Callable[[FileWatchEvent], None]
type SnapshotCallback = Callable[[DeckSnapshot], None]
type CollectionStage = Literal["list", "version", "history", "read", "verify", "deliver"]


@dataclass(frozen=True)
class CollectionFailure:
    """Plain failure data; never carry an exception or a COM proxy across threads."""

    source_path: Path | None
    expected_sha256: str | None
    stage: CollectionStage
    error_type: str
    message: str
    attempt: int
    retrying: bool


type CollectionFailureCallback = Callable[[CollectionFailure], None]


class SavedFileSource(Protocol):
    def list_paths(self) -> tuple[Path, ...]: ...

    def current_revision(self, path: Path) -> SavedRevision | None: ...


class SuccessfulAnalysisHashes(Protocol):
    """Read-only history of completed checks, never of detection or extraction."""

    def last_successful_sha256(self, path: Path) -> str | None: ...


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
