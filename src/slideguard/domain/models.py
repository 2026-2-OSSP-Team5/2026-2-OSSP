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
    # color remains safe for legacy opaque-color checks. Other fills keep their
    # measured source RGB separately, without pretending it is the visible RGB.
    source_color: Color | None = None
    transparency: float | None = None
    fill_kind: str | None = None
    fill_visible: bool | None = None


@dataclass(frozen=True)
class ObjectLocator:
    """Address inside one saved revision; indices are one-based COM positions.

    shape_path contains the top-level shape ID and any enclosing group child IDs.
    kind identifies shape/table_cell/chart_title/axis_title/data_label/smartart_node.
    indices contain row+column, axis type+group, series+point, or node+shape position.
    """

    shape_path: tuple[int, ...]
    kind: str = "shape"
    indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class Shape:
    slide_number: int
    shape_id: int
    name: str
    box: Box | None
    text: str = ""
    runs: tuple[TextRun, ...] = ()
    background: Color | None = None
    rendered_box: Box | None = None
    supported: bool = False
    notes: tuple[str, ...] = ()
    locator: ObjectLocator | None = None

    @property
    def key(self) -> str:
        if self.locator is not None:
            address = self.locator
            if address.kind != "shape" or len(address.shape_path) > 1:
                path = "/".join(str(value) for value in address.shape_path)
                indices = ",".join(str(value) for value in address.indices)
                return f"{self.slide_number}:{path}:{address.kind}:{indices}"
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
    schema_version: str = "0.2"
