"""팀원 코드가 아직 없어서 쓰는 '임시 대역' 모델 (테스트 전용).

src 쪽 코드는 이 파일을 import하지 않습니다.
"""

from dataclasses import dataclass, field


@dataclass
class FakeRun:  # A의 TextRun 대역
    text: str
    size_pt: float | None = None
    color: str | None = None
    bold: bool | None = None


@dataclass
class FakeShape:  # A의 Shape 대역 (좌표 단위 pt)
    slide_index: int
    shape_id: int
    runs: list = field(default_factory=list)
    left: float | None = 50
    top: float | None = 50
    width: float | None = 400
    height: float | None = 100
    kind: str | None = None  # None이면 글자가 있으면 "text"
    supported: bool = True


@dataclass
class FakeSnapshot:  # A의 DeckSnapshot 대역
    shapes: list = field(default_factory=list)
    path: str = "a.pptx"
    sha256: str = "abc"
    slide_width: float = 960
    slide_height: float = 540


@dataclass
class FakeFinding:  # B의 Finding 대역
    rule_id: str
    slide_index: int
    shape_id: int
    status: str = "warning"
    measured: dict = field(default_factory=dict)


@dataclass
class FakePolicy:  # B의 AnalysisPolicy 대역
    strength: str = "balanced"
    purpose: str = "presentation"
    locked_shapes: frozenset = frozenset()
    locked_properties: frozenset = frozenset()
    standard_overrides: dict = field(default_factory=dict)
    user_locks: tuple = ()  # 사용자 지정 체크박스: "font_size", "layout" …
    excluded_rules: tuple = ()  # 검사 제외: "contrast" …
