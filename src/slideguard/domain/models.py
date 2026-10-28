from dataclasses import dataclass, field
from typing import Literal

Color = tuple[int, int, int]
Severity = Literal["warning", "review", "unknown"]


@dataclass(frozen=True)
class Box:
    """Coordinates in PowerPoint points (72 pt = 1 inch)."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class TextRun:
    text: str
    font_size_pt: float | None = None
    color: Color | None = None
    bold: bool | None = None


@dataclass(frozen=True)
class Shape:
    slide_number: int
    shape_id: int
    name: str
    box: Box
    text: str = ""
    runs: tuple[TextRun, ...] = ()
    background: Color | None = None
    rendered_box: Box | None = None
    supported: bool = False
    notes: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.slide_number}:{self.shape_id}"


@dataclass(frozen=True)
class DeckSnapshot:
    source_path: str
    source_sha256: str
    slide_count: int
    width_pt: float
    height_pt: float
    shapes: tuple[Shape, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    id: str
    rule_id: str
    shape_key: str
    severity: Severity
    message: str
    measured: float | str | None
    expected: float | str | None
    evidence: str
    box: Box | None = None


@dataclass(frozen=True)
class ChangeProposal:
    id: str
    finding_id: str
    shape_key: str
    property: str
    before: float | str | Color
    after: float | str | Color
    cost: float


@dataclass
class AnalysisReport:
    deck: DeckSnapshot
    findings: list[Finding] = field(default_factory=list)
    proposals: list[ChangeProposal] = field(default_factory=list)
    executed_rules: tuple[str, ...] = ()
    status: Literal["not_configured", "analyzed"] = "not_configured"
    schema_version: str = "0.1"
